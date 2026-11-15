"""命令行：名字解析和参数解析。

「匹配到多条就拒绝执行」是这个工具最重要的安全行为之一 —— 用户写了个模糊
的名字，我们不能替他猜。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from pcxray import autostart, cli


def entry(name, kind="run", source="测试"):
    return autostart.Entry(name=name, kind=kind, source=source)


@contextlib.contextmanager
def quiet_stderr():
    """argparse 报错会往 stderr 打字，测试里不需要看它。"""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        yield buffer


@contextlib.contextmanager
def quiet_stdout():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry("OneDrive"),
            entry("ClashVerge"),
            entry("MicrosoftEdgeAutoLaunch_A969"),
            entry("MicrosoftEdgeAutoLaunch_29EB"),
            entry("Weixin", kind="task"),
        ]

    def test_exact_name(self):
        hit, candidates = cli.resolve(self.entries, "OneDrive")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, "OneDrive")
        self.assertEqual(candidates, [])

    def test_case_insensitive(self):
        hit, _ = cli.resolve(self.entries, "onedrive")
        self.assertIsNotNone(hit)

    def test_by_key(self):
        key = self.entries[1].key
        hit, _ = cli.resolve(self.entries, key)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, "ClashVerge")

    def test_unique_partial(self):
        hit, _ = cli.resolve(self.entries, "Clash")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, "ClashVerge")

    def test_ambiguous_partial_returns_candidates(self):
        hit, candidates = cli.resolve(self.entries, "MicrosoftEdge")
        self.assertIsNone(hit, "有歧义时必须返回 None，不能随便挑一条")
        self.assertEqual(len(candidates), 2)

    def test_no_match(self):
        hit, candidates = cli.resolve(self.entries, "完全不存在的名字")
        self.assertIsNone(hit)
        self.assertEqual(candidates, [])

    def test_empty_query(self):
        hit, candidates = cli.resolve(self.entries, "   ")
        self.assertIsNone(hit)
        self.assertEqual(candidates, [])

    def test_duplicate_names_are_ambiguous(self):
        entries = [entry("Same", kind="run"), entry("Same", kind="task")]
        hit, candidates = cli.resolve(entries, "Same")
        self.assertIsNone(hit)
        self.assertEqual(len(candidates), 2)


class TestParser(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_list_defaults(self):
        args = self.parser.parse_args(["list"])
        self.assertEqual(args.command, "list")
        self.assertEqual(args.limit, 0)
        self.assertFalse(args.json)

    def test_off_requires_names(self):
        with quiet_stderr(), self.assertRaises(SystemExit):
            self.parser.parse_args(["off"])

    def test_off_accepts_multiple(self):
        args = self.parser.parse_args(["off", "a", "b"])
        self.assertEqual(args.names, ["a", "b"])

    def test_mode_choices(self):
        args = self.parser.parse_args(["off", "x", "--mode", "move"])
        self.assertEqual(args.mode, "move")
        with quiet_stderr(), self.assertRaises(SystemExit):
            self.parser.parse_args(["off", "x", "--mode", "nonsense"])

    def test_restore_backup_optional(self):
        args = self.parser.parse_args(["restore"])
        self.assertEqual(args.backup, "")

    def test_web_port_default(self):
        args = self.parser.parse_args(["web"])
        self.assertEqual(args.port, 0)

    def test_dry_run_flag(self):
        args = self.parser.parse_args(["off", "x", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_kind_and_include(self):
        args = self.parser.parse_args(["list", "--kind", "task", "--include", "registry,folders"])
        self.assertEqual(args.kind, "task")
        self.assertEqual(args.include, "registry,folders")


class TestTruncationIsDisclosed(unittest.TestCase):
    """截断必须说出来。

    `--limit 3` 只给 3 行、不提还有多少条，用户会以为「这一类就 3 条」。
    """

    def _run(self, argv):
        with quiet_stdout() as buffer:
            code = cli.main(argv)
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_list_says_how_many_are_hidden(self):
        out = self._run(["list", "--limit", "2"])
        self.assertIn("只显示了前 2 条", out)
        self.assertIn("--limit 0", out)

    def test_list_limit_zero_says_nothing_about_truncation(self):
        self.assertNotIn("只显示了前", self._run(["list", "--limit", "0"]))

    def test_ram_says_how_many_are_hidden(self):
        self.assertIn("只显示了占内存最多的 2 个程序", self._run(["ram", "--limit", "2"]))

    def test_ram_limit_zero_really_means_all(self):
        """`--limit 0` 在两条命令里必须同一个意思：全部。

        以前 ram 用 `args.limit or 25`，0 被当成「没写」，只显示 25 行，
        还反过来劝用户「用 --limit 0 看全部」—— 用户照做也没用。
        """
        self.assertNotIn("只显示了", self._run(["ram", "--limit", "0"]))


class TestListArgumentValidation(unittest.TestCase):
    """坏参数要当场报错。

    以前 `--kind nope` 会静默输出一份正常报告 —— 用户以为「这一类没有条目」，
    其实是他把类型名打错了。`--limit -1` 更隐蔽：`shown[:-1]` 少掉最后一行，
    看起来就像「数据就这么点」。
    """

    def setUp(self):
        self.parser = cli.build_parser()

    def _run(self, argv):
        args = self.parser.parse_args(argv)
        with mock.patch.object(cli, "_load_entries", return_value=([], [])), mock.patch.object(
            cli.winapi, "processes", return_value=[]
        ), mock.patch.object(cli.winapi, "memory_status", return_value=(0, 0)), mock.patch.object(
            cli.winapi, "boot_time", return_value=0.0
        ), mock.patch.object(cli.procs, "logon_time", return_value=0.0), mock.patch.object(
            cli.report_mod, "build", side_effect=lambda **kw: _empty_report()
        ):
            with quiet_stdout(), quiet_stderr():
                return args.func(args)

    def test_negative_limit_is_rejected(self):
        self.assertEqual(self._run(["list", "--limit", "-1"]), 2)

    def test_unknown_kind_is_rejected(self):
        self.assertEqual(self._run(["list", "--kind", "nope"]), 2)

    def test_unknown_include_is_rejected(self):
        # 静默忽略会变成「扫了 0 条」，用户以为系统很干净
        self.assertEqual(self._run(["list", "--include", "nope"]), 2)

    def test_one_bad_include_rejects_the_whole_call(self):
        self.assertEqual(self._run(["list", "--include", "registry,nope"]), 2)

    def test_every_documented_include_is_accepted(self):
        for part in cli._KNOWN_INCLUDE:
            with self.subTest(part=part):
                self.assertEqual(self._run(["list", "--include", part]), 0)

    def test_limit_zero_means_unlimited(self):
        self.assertEqual(self._run(["list", "--limit", "0"]), 0)

    def test_every_documented_kind_is_accepted(self):
        for kind in cli._KNOWN_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(self._run(["list", "--kind", kind]), 0)

    def test_known_kinds_match_what_the_scanner_emits(self):
        # 这两边一旦不同步，用户就会遇到「文档里写了但 --kind 不认」
        from pcxray import autostart

        emitted = set(autostart._KIND_ORDER) if hasattr(autostart, "_KIND_ORDER") else set()
        if emitted:
            self.assertTrue(set(cli._KNOWN_KINDS) >= emitted, emitted - set(cli._KNOWN_KINDS))


def _empty_report():
    from pcxray import report as report_mod

    rep = report_mod.Report()
    return rep


class TestPick(unittest.TestCase):
    def setUp(self):
        self.entries = [entry("A"), entry("B")]

    def test_picks_multiple(self):
        picked, problems = cli._pick(self.entries, ["A", "B"])
        self.assertEqual(len(picked), 2)
        self.assertEqual(problems, [])

    def test_reports_missing(self):
        picked, problems = cli._pick(self.entries, ["A", "不存在"])
        self.assertEqual(len(picked), 1)
        self.assertEqual(len(problems), 1)
        self.assertFalse(problems[0].ok)
        self.assertIn("没有这一条", problems[0].message)

    def test_ambiguous_message_lists_candidates(self):
        entries = [entry("Edge_1"), entry("Edge_2")]
        _picked, problems = cli._pick(entries, ["Edge"])
        self.assertEqual(len(problems), 1)
        self.assertIn("匹配到 2 条", problems[0].message)


if __name__ == "__main__":
    unittest.main()
