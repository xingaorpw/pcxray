"""命令行：名字解析和参数解析。

「匹配到多条就拒绝执行」是这个工具最重要的安全行为之一 —— 用户写了个模糊
的名字，我们不能替他猜。
"""

from __future__ import annotations

import contextlib
import json
import io
import unittest
from unittest import mock

from pcxray import autostart, cli, procs


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


class TestAmbiguousCandidatesAreCapped(unittest.TestCase):
    """匹配到多条时只列前 4 条。

    六个名字、每个六十多个字符，一次全列出来是一堵墙 —— 看不完也抄不下来。
    """

    def _pick(self, names, query):
        entries = [
            autostart.Entry(name=n, kind="run", source="注册表 · 当前用户 Run", scope="user")
            for n in names
        ]
        return cli._pick(entries, [query])

    def test_lists_at_most_four_and_says_how_many_more(self):
        picked, problems = self._pick([f"MicrosoftEdgeAutoLaunch_{i:04d}" for i in range(9)], "MicrosoftEdge")
        self.assertEqual(picked, [])
        self.assertEqual(len(problems), 1)
        message = problems[0].message
        self.assertIn("匹配到 9 条", message)
        self.assertIn("还有 5 条", message)
        self.assertIn("pcxray list --kind run", message)

    def test_four_or_fewer_lists_them_all_without_the_more_hint(self):
        _picked, problems = self._pick([f"thing{i}" for i in range(3)], "thing")
        self.assertEqual(len(problems), 1)
        self.assertIn("匹配到 3 条", problems[0].message)
        self.assertNotIn("还有", problems[0].message)


class TestResolveIgnoresSeparators(unittest.TestCase):
    """用户记不住名字里的下划线和空格，敲 `clash verge` 就该能找到
    `ClashVerge`。

    只在「去掉分隔符后完全相等」时命中，不做模糊 —— 关错东西的代价太大。
    """

    def setUp(self):
        def entry(name, kind="run"):
            return autostart.Entry(name=name, kind=kind, source="用户 Run", scope="user")

        self.entries = [
            entry("ClashVerge"),
            entry("clash_verge_service", kind="service"),
            entry("OneDrive"),
            entry("MicrosoftEdgeAutoLaunch_A96907E98764DB"),
        ]

    def test_space_matches_underscoreless_name(self):
        hit, _cands = cli.resolve(self.entries, "clash verge")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, "ClashVerge")

    def test_hyphen_and_dot_are_ignored_too(self):
        hit, _cands = cli.resolve(self.entries, "one.drive")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, "OneDrive")

    def test_does_not_fuzzy_match_the_wrong_thing(self):
        # 「clash verge」不该匹配到 clash_verge_service（那是个服务，关错代价大）
        hit, _cands = cli.resolve(self.entries, "clash verge")
        self.assertNotEqual(hit.kind if hit else None, "service")

    def test_ambiguous_after_squashing_returns_candidates(self):
        entries = self.entries + [
            autostart.Entry(name="clash-verge", kind="run", source="用户 Run", scope="user")
        ]
        hit, cands = cli.resolve(entries, "clash verge")
        self.assertIsNone(hit)
        self.assertEqual(len(cands), 2)

    def test_unknown_still_returns_nothing(self):
        hit, cands = cli.resolve(self.entries, "绝对不存在")
        self.assertIsNone(hit)
        self.assertEqual(cands, [])


class TestMixedNamesAreAllReported(unittest.TestCase):
    """名字里混了一个不存在的，必须两条都报出来。

    以前只有「一个都没找到」时才打印问题列表；混着写就只报成功那条、退出码 0，
    用户以为两个都办了。
    """

    def test_off_reports_the_missing_name_too(self):
        with quiet_stdout() as buffer:
            code = cli.main(["off", "绝对不存在_xyz", "--dry-run"])
        out = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("绝对不存在_xyz", out)

    def test_off_with_one_good_and_one_bad_exits_nonzero(self):
        entries = autostart.scan(("registry",))[0]
        good = next((e for e in entries if e.kind == "run" and not e.protected), None)
        if good is None:
            self.skipTest("这台机器上没有可用的用户 Run 条目")
        with quiet_stdout() as buffer:
            code = cli.main(["off", good.name, "绝对不存在_xyz", "--dry-run"])
        out = buffer.getvalue()
        self.assertEqual(code, 1, out)
        self.assertIn("绝对不存在_xyz", out)
        self.assertIn(good.name, out)


class _StubReport:
    def __init__(self):
        self.problems: list[str] = []


class TestFilterArgumentsAreForgiving(unittest.TestCase):
    """逗号后面带空格、随手大写，都不是用户的错。

    `--include "registry, folders"` 以前会被当成「不认识的 --include： folders」，
    提示里还带着那个前导空格，看着像工具坏了。
    """

    def test_include_accepts_spaces_and_case(self):
        self.assertEqual(cli._parse_include("registry, folders"), ["registry", "folders"])
        self.assertEqual(cli._parse_include(" REGISTRY ,Folders "), ["registry", "folders"])
        self.assertEqual(cli._parse_include("registry,registry"), ["registry"])

    def test_include_with_space_is_accepted_by_the_cli(self):
        captured = io.StringIO()
        with mock.patch.object(cli.autostart, "scan", return_value=([], [])) as scan, mock.patch.object(
            cli.winapi, "processes", return_value=[]
        ), contextlib.redirect_stdout(captured):
            code = cli.main(["list", "--include", "registry, folders"])
        self.assertEqual(code, 0)
        self.assertEqual(scan.call_args[0][0], ("registry", "folders"))

    def test_unknown_include_still_rejected(self):
        err = io.StringIO()
        with mock.patch.object(cli.autostart, "scan", return_value=([], [])), contextlib.redirect_stderr(err):
            code = cli.main(["list", "--include", "registry, nope"])
        self.assertEqual(code, 2)
        self.assertIn("nope", err.getvalue())

    def test_kind_is_case_and_space_insensitive(self):
        for value in ("task", "TASK", " Task "):
            captured = io.StringIO()
            with mock.patch.object(cli.autostart, "scan", return_value=([], [])), mock.patch.object(
                cli.winapi, "processes", return_value=[]
            ), contextlib.redirect_stdout(captured):
                code = cli.main(["list", "--kind", value])
            self.assertEqual(code, 0, value)


class TestEmptyNameIsExplained(unittest.TestCase):
    """`pcxray off ""` 是引号没写对时最容易出现的输入。

    原来的提示是「✗ 　没有这一条」，名字那一段是空的，看不出是哪里错了。
    """

    def test_empty_name_gets_its_own_message(self):
        picked, problems = cli._pick([], ["", "   "])
        self.assertEqual(picked, [])
        self.assertEqual(len(problems), 2)
        for problem in problems:
            self.assertIn("名字不能是空的", problem.message)

    def test_off_with_empty_name_exits_nonzero(self):
        out = io.StringIO()
        with mock.patch.object(cli, "_load_entries", return_value=([], [])), contextlib.redirect_stdout(out):
            code = cli.main(["off", "", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("名字不能是空的", out.getvalue())


class TestPruneCommand(unittest.TestCase):
    def test_zero_keep_exits_2(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["prune", "--keep", "0"])
        self.assertEqual(code, 2)
        self.assertIn("至少是 1", err.getvalue())

    def test_nothing_to_do_says_so(self):
        captured = io.StringIO()
        with mock.patch.object(cli.actions, "prune", return_value=([], 3, 0)), contextlib.redirect_stdout(
            captured
        ):
            code = cli.main(["prune"])
        self.assertEqual(code, 0)
        self.assertIn("不用清", captured.getvalue())

    def test_dry_run_says_nothing_was_deleted(self):
        captured = io.StringIO()
        removed = [f"/x/2026010{i}-000000.json" for i in range(3)]
        with mock.patch.object(
            cli.actions, "prune", return_value=(removed, 20, 4096)
        ), contextlib.redirect_stdout(captured):
            code = cli.main(["prune", "--dry-run"])
        self.assertEqual(code, 0)
        text = captured.getvalue()
        self.assertIn("将要删掉 3 份", text)
        self.assertIn("什么都没删", text)


class TestNarrowTerminalIsCalledOut(unittest.TestCase):
    """表格比窗口宽时要说一句。

    折行之后表头和数据会错位，看起来像排版坏了 —— 用户会以为是程序的问题。
    """

    def _note(self, columns, lines):
        class Size:
            pass

        size = Size()
        size.columns = columns
        with mock.patch.object(cli.shutil, "get_terminal_size", return_value=size):
            return cli._width_note(lines)

    def test_wide_table_on_a_narrow_terminal_gets_a_note(self):
        lines = ["  " + "x" * 96]
        note = self._note(40, lines)
        self.assertIn("会折行", note)
        # 行首那两个空格也算宽度，所以是 98
        self.assertIn("98", note)
        self.assertIn("40", note)

    def test_table_that_fits_gets_no_note(self):
        self.assertEqual(self._note(200, ["  " + "x" * 96]), "")

    def test_cjk_width_is_measured_not_guessed(self):
        # 48 个汉字 = 96 列，加行首两个空格 = 98；按 len() 算只有 50，会漏报
        lines = ["  " + "汉" * 48]
        self.assertIn("98", self._note(80, lines))

    def test_no_terminal_means_no_note(self):
        self.assertEqual(self._note(0, ["  " + "x" * 200]), "")

    def test_list_prints_the_note(self):
        captured = io.StringIO()
        with mock.patch.object(cli, "_load_entries", return_value=([], [])), mock.patch.object(
            cli.winapi, "processes", return_value=[]
        ), mock.patch.object(cli.report_mod, "build", return_value=_StubReport()), mock.patch.object(
            cli.report_mod, "render", return_value=["  " + "x" * 120]
        ), mock.patch.object(
            cli.shutil, "get_terminal_size", return_value=type("S", (), {"columns": 40})()
        ), contextlib.redirect_stdout(captured):
            code = cli.main(["list"])
        self.assertEqual(code, 0)
        self.assertIn("会折行", captured.getvalue())


class TestRamJsonHonoursLimit(unittest.TestCase):
    """`ram --json` 的 limit 语义要和 `list --json` 一致。

    `--limit` 不写时表格只给 25 行，但脚本要的是全量 —— 所以「没写」和「写了
    25」必须区分开，JSON 路径不能被表格的默认值截掉。
    """

    def setUp(self):
        self.groups = []
        for i in range(40):
            group = procs.Group(f"p{i}")
            group.pids = [i + 1]
            group.mb = 100.0 - i
            group.private_mb = 50.0
            self.groups.append(group)

    def _run(self, argv):
        captured = io.StringIO()
        with mock.patch.object(cli.winapi, "processes", return_value=[]), mock.patch.object(
            cli.procs, "group", return_value=self.groups
        ), mock.patch.object(cli.procs, "logon_time", return_value=0.0), contextlib.redirect_stdout(captured):
            code = cli.main(argv)
        return code, captured.getvalue()

    def test_no_limit_gives_every_group(self):
        code, out = self._run(["ram", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["groups"]), 40)
        self.assertEqual(data["hidden_by_limit"], 0)
        self.assertEqual(data["group_count"], 40)

    def test_explicit_limit_truncates_and_says_so(self):
        code, out = self._run(["ram", "--json", "--limit", "3"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["groups"]), 3)
        self.assertEqual(data["hidden_by_limit"], 37)
        self.assertEqual(data["group_count"], 40)

    def test_limit_zero_means_all(self):
        code, out = self._run(["ram", "--json", "--limit", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["groups"]), 40)


class TestNegativeLimitsAreRejected(unittest.TestCase):
    """负的 --limit 不能静默丢数据。

    `shown[:-5]` 会悄悄少掉最后 5 行，看起来像「数据就这么点」。
    """

    def test_list_rejects_negative_limit(self):
        with quiet_stdout(), quiet_stderr() as err:
            code = cli.main(["list", "--limit", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("--limit 不能是负数", err.getvalue())

    def test_ram_rejects_negative_limit(self):
        with quiet_stdout(), quiet_stderr() as err:
            code = cli.main(["ram", "--limit", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("--limit 不能是负数", err.getvalue())


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
