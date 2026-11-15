"""命令行：名字解析和参数解析。

「匹配到多条就拒绝执行」是这个工具最重要的安全行为之一 —— 用户写了个模糊
的名字，我们不能替他猜。
"""

from __future__ import annotations

import contextlib
import io
import unittest

from pcxray import autostart, cli


def entry(name, kind="run", source="测试"):
    return autostart.Entry(name=name, kind=kind, source=source)


@contextlib.contextmanager
def quiet_stderr():
    """argparse 报错会往 stderr 打字，测试里不需要看它。"""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
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
