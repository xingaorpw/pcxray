"""报告合成的逻辑测试。用假数据，不需要 Windows。"""

from __future__ import annotations

import unittest
from unittest import mock

from pcxray import autostart, report, winapi


def proc(pid, name, mb=0.0, path="", visible=False, started=0.0, readable=True):
    return winapi.Process(
        pid=pid,
        name=name,
        path=path,
        working_set=int(mb * 1048576),
        private=int(mb * 1048576),
        started=started,
        windows=winapi.Windows(total=1 if visible else 0, visible=1 if visible else 0),
        readable=readable,
    )


def entry(name, kind="run", scope="user", exe="", enabled=True, **kwargs):
    kwargs.setdefault("command", exe)
    return autostart.Entry(
        name=name,
        kind=kind,
        source="测试",
        scope=scope,
        exe=exe,
        enabled=enabled,
        **kwargs,
    )


class TestShortSourceAndMarks(unittest.TestCase):
    """来源标签和行内标记以前有几条分支没被渲染测试碰到。

    这些标记是用户判断「这条要不要关」的依据之一：漏一个「!可疑」等于把
    一条可疑项伪装成正常项。
    """

    def _entry(self, **kwargs):
        base = dict(name="X", kind="run", source="注册表 · 当前用户 Run", scope="user")
        base.update(kwargs)
        return autostart.Entry(**base)

    def test_source_labels_cover_every_kind(self):
        cases = {
            ("run", "user"): "用户 Run",
            ("run", "machine"): "所有用户 Run",
            ("runonce", "user"): "RunOnce",
            ("run32", "machine"): "所有用户 Run32",
            ("task", "user"): "计划任务",
            ("service", "machine"): "服务",
        }
        for (kind, scope), expected in cases.items():
            with self.subTest(kind=kind, scope=scope):
                got = report._short_source(self._entry(kind=kind, scope=scope))
                self.assertEqual(got, expected)

    def test_folder_label_depends_on_scope(self):
        self.assertEqual(report._short_source(self._entry(kind="folder", scope="user")), "用户启动夹")
        self.assertEqual(report._short_source(self._entry(kind="folder", scope="machine")), "公共启动夹")

    def _item(self, **fields):
        item = report.Item(self._entry())
        for key, value in fields.items():
            setattr(item, key, value)
        return item

    def test_hidden_mark_is_rendered(self):
        item = self._item(exists=True, hidden=True, company="某公司")
        self.assertIn("不显示窗口", item.flags)

    def test_suspect_mark_is_rendered(self):
        item = self._item(exists=True, suspect="在临时目录里", company="某公司")
        self.assertIn("可疑路径", item.flags)

    def test_missing_file_mark_is_rendered(self):
        item = self._item(exists=False)
        self.assertIn("文件不存在", item.flags)

    def test_running_mark_is_rendered(self):
        item = self._item(exists=True, runs=[object()], company="某公司")
        self.assertIn("正在运行", item.flags)

    def test_shared_program_is_explained(self):
        # 两条自启项指向同一个 exe 时，同一笔内存会在两行里各出现一次
        item = self._item(exists=True, shared=2, company="某公司")
        self.assertIn("同一程序 2 条", item.flags)

    def test_missing_publisher_is_flagged(self):
        item = self._item(exists=True, company="")
        self.assertIn("无发行商", item.flags)

    def test_caveat_from_scanner_reaches_the_table(self):
        # 扫描器发现但没法用短标签概括的情况写在 entry.caveat 里；
        # 以前这些只进 JSON，命令行一个字都不显示
        item = report.Item(self._entry(caveat="没有命令行，靠 COM 启动"))
        self.assertIn("没有命令行，靠 COM 启动", item.flags)

    def test_render_shows_the_marks_in_the_row(self):
        item = self._item(exists=False, suspect="在临时目录里", hidden=True, company="")
        rep = report.Report()
        rep.items = [item]
        lines = report.render(rep)
        row = next(line for line in lines if "X" in line and "!" in line)
        self.assertIn("!文件没了", row)
        self.assertIn("!可疑", row)
        self.assertIn("不显窗", row)


class TestBuildScansWhenGivenNothing(unittest.TestCase):
    """`build()` 不传 entries 时要自己扫，并且把「读不到」带上。

    以前只测过「传 entries」的路径，`build()` 自己调 scan 的那段没人走过。
    """

    def test_build_without_entries_scans_and_keeps_problems(self):
        entries = [
            autostart.Entry(name="X", kind="run", source="注册表 · 当前用户 Run", scope="user")
        ]
        with mock.patch.object(report.autostart, "scan", return_value=(entries, ["服务：读不到"])) as scan, mock.patch.object(
            report.winapi, "processes", return_value=[]
        ), mock.patch.object(report.winapi, "is_admin", return_value=False):
            rep = report.build()
        scan.assert_called_once()
        self.assertEqual(len(rep.items), 1)
        self.assertIn("服务：读不到", rep.problems)


class Base(unittest.TestCase):
    def setUp(self):
        self.patchers = [
            mock.patch.object(winapi, "is_admin", return_value=False),
            mock.patch.object(winapi, "memory_status", return_value=(16 << 30, 6 << 30)),
            mock.patch.object(winapi, "boot_time", return_value=1000.0),
            mock.patch.object(winapi, "COVERAGE", {"mode": "all", "reason": ""}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, entries, procs):
        return report.build(entries=entries, process_list=procs)


class TestBuild(Base):
    def test_counts(self):
        rep = self.build(
            [entry("a"), entry("b", enabled=False)],
            [proc(1, "explorer.exe", 100, visible=True, started=2000)],
        )
        self.assertEqual(len(rep.items), 2)
        self.assertEqual(len(rep.enabled), 1)
        self.assertEqual(len(rep.disabled), 1)

    def test_logon_from_explorer(self):
        rep = self.build([], [proc(1, "explorer.exe", 10, visible=True, started=555)])
        self.assertEqual(rep.logon, 555)

    def test_ram_numbers(self):
        rep = self.build([], [])
        self.assertEqual(rep.total_ram, 16 << 30)
        self.assertEqual(rep.avail_ram, 6 << 30)

    def test_non_admin_adds_a_note(self):
        # 只有真的读不到进程时才提示 —— 一个都读不到（或没有进程）时说
        # 「不是管理员：0 个进程读不到」纯属噪音，用户会以为自己漏看了什么
        rep = self.build([], [proc(1, "a.exe", 10, readable=False)])
        lines = "\n".join(report.render(rep))
        self.assertIn("不是管理员", lines)
        self.assertIn("1 个进程读不到", lines)

    def test_no_note_when_nothing_is_unreadable(self):
        rep = self.build([], [])
        lines = "\n".join(report.render(rep))
        self.assertNotIn("不是管理员", lines)

    def test_admin_never_gets_the_note(self):
        with mock.patch.object(winapi, "is_admin", lambda: True):
            rep = self.build([], [proc(1, "a.exe", 10, readable=False)])
        self.assertNotIn("不是管理员", "\n".join(report.render(rep)))

    def test_coverage_problem_is_surfaced(self):
        with mock.patch.object(winapi, "COVERAGE", {"mode": "partial", "reason": "自检没过"}):
            rep = self.build([], [])
        self.assertIn("自检没过", rep.problems)
        self.assertIn("自检没过", "\n".join(report.render(rep)))


class TestAnnotation(Base):
    def test_exe_missing_flagged(self):
        rep = self.build([entry("x", exe=r"C:\no\such\file_zzz.exe")], [])
        self.assertFalse(rep.items[0].exists)
        self.assertIn("文件不存在", rep.items[0].flags)

    def test_suspect_path_flagged(self):
        rep = self.build([entry("x", exe=r"C:\Users\a\AppData\Local\Temp\x.exe")], [])
        self.assertTrue(rep.items[0].suspect)
        self.assertIn("可疑路径", rep.items[0].flags)

    def test_hidden_args_flagged(self):
        rep = self.build([entry("x", exe="a.exe", command="a.exe --silent")], [])
        self.assertTrue(rep.items[0].hidden)

    def test_running_matched(self):
        rep = self.build(
            [entry("clash", exe=r"C:\Program Files\Clash\clash.exe")],
            [proc(7, "clash.exe", 25, path=r"C:\Program Files\Clash\clash.exe")],
        )
        self.assertEqual(len(rep.items[0].runs), 1)
        self.assertAlmostEqual(rep.items[0].run_mb, 25)
        self.assertIn("正在运行", rep.items[0].flags)

    def test_protected_task_refused(self):
        rep = self.build([entry("t", kind="task", protected=True)], [])
        self.assertIn("系统自带", rep.items[0].refuse)
        self.assertFalse(rep.items[0].as_dict()["can_disable"])

    def test_service_needs_force_flag(self):
        # 非管理员时服务先被「需要管理员」挡住；管理员下才是「需确认」
        rep = self.build([entry("s", kind="service", scope="machine")], [])
        self.assertTrue(rep.items[0].refuse)  # 非管理员
        with mock.patch.object(winapi, "is_admin", return_value=True):
            rep2 = self.build([entry("s", kind="service", scope="machine")], [])
        self.assertEqual(rep2.items[0].refuse, "")
        self.assertTrue(rep2.items[0].needs_force)
        self.assertIn("需确认", rep2.items[0].flags)


class TestAutostartMemory(Base):
    def test_dedupes_by_pid(self):
        # 两个 Edge 自启项指向同一个 exe：不能把同一批进程算两遍
        exe = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        procs = [proc(11, "msedge.exe", 100, path=exe), proc(12, "msedge.exe", 50, path=exe)]
        rep = self.build([entry("edge1", exe=exe), entry("edge2", exe=exe)], procs)
        self.assertEqual(len(rep.items[0].runs), 2)
        self.assertAlmostEqual(rep.autostart_memory(), 150)

    def test_disabled_entries_not_counted(self):
        exe = r"C:\a\x.exe"
        rep = self.build(
            [entry("x", exe=exe, enabled=False)],
            [proc(1, "x.exe", 100, path=exe)],
        )
        self.assertEqual(rep.autostart_memory(), 0)

    def test_login_window_counted_separately(self):
        rep = self.build(
            [],
            [proc(1, "a.exe", 40, started=1000), proc(2, "b.exe", 60, started=9000)],
        )
        self.assertEqual(rep.auto_count, 1)
        self.assertAlmostEqual(rep.auto_mb, 40)


class TestRender(Base):
    def test_renders_without_crash_and_has_sections(self):
        exe = r"C:\Program Files\X\x.exe"
        rep = self.build(
            [entry("x", exe=exe), entry("t", kind="task")],
            [proc(1, "explorer.exe", 100, visible=True, started=1000), proc(2, "x.exe", 200, path=exe)],
        )
        out = "\n".join(report.render(rep))
        self.assertIn("pcxray", out)
        self.assertIn("开机自启", out)
        self.assertIn("还有：计划任务", out)

    def test_only_filters(self):
        rep = self.build([entry("x"), entry("t", kind="task")], [])
        out = "\n".join(report.render(rep, only="task"))
        self.assertIn("计划任务", out)
        self.assertNotIn("还有：", out)

    def test_limit(self):
        rep = self.build([entry(f"x{i}") for i in range(10)], [])
        out = report.render(rep, limit=3)
        self.assertLessEqual(len([l for l in out if "x" in l]), 5)

    def test_empty_does_not_crash(self):
        rep = self.build([], [])
        self.assertTrue(report.render(rep))

    def test_background_section_lists_programs(self):
        rep = self.build(
            [],
            [proc(1, "big.exe", 500, path=r"C:\big.exe", visible=False, started=1000)],
        )
        out = "\n".join(report.render(rep))
        self.assertIn("后台占内存", out)
        self.assertIn("big.exe", out)

    def test_unreadable_path_shown_as_such(self):
        rep = self.build(
            [],
            [proc(1, "sys.exe", 500, path="", visible=False, readable=False, started=1000)],
        )
        out = "\n".join(report.render(rep))
        self.assertIn("读不到路径", out)


class TestAsDict(Base):
    def test_json_ready(self):
        import json

        rep = self.build([entry("x", exe=r"C:\a\x.exe")], [proc(1, "x.exe", 10, path=r"C:\a\x.exe")])
        text = json.dumps(rep.as_dict(), ensure_ascii=False)
        self.assertIn("autostart", text)
        self.assertIn("x.exe", text)


if __name__ == "__main__":
    unittest.main()
