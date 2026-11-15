"""报告里那些数字必须自己跟自己说得通。

一个工具最伤信任的不是「少显示一条」，而是**头部合计和表格里的行加起来对不上**，
用户没法判断该信哪个数。这里把几条不变式钉住。
"""

from __future__ import annotations

import unittest
from unittest import mock

from pcxray import autostart, procs, report, winapi
from pcxray.report import render


def fake_proc(pid: int, path: str, mb: float) -> winapi.Process:
    return winapi.Process(
        pid=pid,
        name=path.rsplit("\\", 1)[-1],
        path=path,
        working_set=int(mb * 1048576),
    )


def entry(name: str, exe: str, enabled: bool = True, kind: str = "run") -> autostart.Entry:
    return autostart.Entry(
        name=name,
        kind=kind,
        source="测试",
        scope="user",
        command=exe,
        exe=exe,
        enabled=enabled,
    )


def build(entries, processes, logon=0.0):
    """绕开真实扫描，只跑 build 的装配逻辑。"""
    with mock.patch.object(report.winapi, "processes", lambda: processes), mock.patch.object(
        report.winapi, "memory_status", lambda: (16 << 30, 8 << 30)
    ), mock.patch.object(report.winapi, "boot_time", lambda: 0.0), mock.patch.object(
        report.procs, "logon_time", lambda _p: logon
    ), mock.patch.object(
        report.winapi, "version_info", lambda _p: {}
    ), mock.patch.object(
        report.os.path, "isfile", lambda _p: True
    ):
        return report.build(entries=entries)


class TestCounts(unittest.TestCase):
    def test_enabled_plus_disabled_is_total(self):
        entries = [entry("A", r"C:\a.exe"), entry("B", r"C:\b.exe", enabled=False)]
        rep = build(entries, [])
        self.assertEqual(len(rep.items), len(entries))
        self.assertEqual(len(rep.enabled) + len(rep.disabled), len(rep.items))

    def test_json_counts_match_the_lists(self):
        entries = [entry("A", r"C:\a.exe"), entry("B", r"C:\b.exe", enabled=False)]
        data = build(entries, []).as_dict()
        self.assertEqual(data["autostart"]["total"], len(data["autostart"]["items"]))
        self.assertEqual(data["autostart"]["enabled"], 1)
        self.assertEqual(data["autostart"]["disabled"], 1)


class TestMemoryAttribution(unittest.TestCase):
    def test_one_owner_sums_exactly(self):
        entries = [entry("A", r"C:\a.exe")]
        procs_list = [fake_proc(10, r"C:\a.exe", 100.0)]
        rep = build(entries, procs_list)
        self.assertEqual(rep.items[0].shared, 1)
        self.assertAlmostEqual(rep.items[0].run_mb, 100.0, places=1)
        self.assertAlmostEqual(rep.autostart_memory(), 100.0, places=1)

    def test_two_entries_on_the_same_exe_do_not_double_count_the_total(self):
        # 本机真实情况：两条 Edge 自启项都指向 msedge.exe
        entries = [entry("Edge A", r"C:\msedge.exe"), entry("Edge B", r"C:\msedge.exe")]
        procs_list = [fake_proc(10, r"C:\msedge.exe", 200.0), fake_proc(11, r"C:\msedge.exe", 300.0)]
        rep = build(entries, procs_list)
        self.assertEqual([i.shared for i in rep.items], [2, 2])
        # 每行都显示这个程序的全部内存（各自 500 MB）
        self.assertAlmostEqual(rep.items[0].run_mb, 500.0, places=1)
        self.assertAlmostEqual(rep.items[1].run_mb, 500.0, places=1)
        # 但合计只算一次
        self.assertAlmostEqual(rep.autostart_memory(), 500.0, places=1)
        self.assertLess(rep.autostart_memory(), sum(i.run_mb for i in rep.items))

    def test_shared_flag_tells_the_user_why(self):
        entries = [entry("Edge A", r"C:\msedge.exe"), entry("Edge B", r"C:\msedge.exe")]
        rep = build(entries, [fake_proc(10, r"C:\msedge.exe", 200.0)])
        self.assertIn("同一程序 2 条", rep.items[0].flags)
        self.assertIn("同一程序 2 条", rep.items[1].flags)

    def test_single_owner_has_no_shared_flag(self):
        rep = build([entry("A", r"C:\a.exe")], [fake_proc(10, r"C:\a.exe", 50.0)])
        self.assertNotIn("同一程序 2 条", rep.items[0].flags)

    def test_disabled_entry_is_excluded_from_the_total(self):
        entries = [entry("A", r"C:\a.exe", enabled=False)]
        rep = build(entries, [fake_proc(10, r"C:\a.exe", 100.0)])
        # 行里还是显示它在跑（这是事实），但不算进「自启项占着多少」
        self.assertAlmostEqual(rep.items[0].run_mb, 100.0, places=1)
        self.assertEqual(rep.autostart_memory(), 0.0)

    def test_mixed_enabled_and_disabled_sharing_a_pid_counts_once(self):
        entries = [entry("On", r"C:\a.exe"), entry("Off", r"C:\a.exe", enabled=False)]
        rep = build(entries, [fake_proc(10, r"C:\a.exe", 80.0)])
        self.assertAlmostEqual(rep.autostart_memory(), 80.0, places=1)

    def test_json_memory_matches_the_python_api(self):
        entries = [entry("A", r"C:\a.exe")]
        rep = build(entries, [fake_proc(10, r"C:\a.exe", 64.0)])
        data = rep.as_dict()
        self.assertAlmostEqual(data["autostart"]["memory_mb"], round(rep.autostart_memory(), 1))
        self.assertAlmostEqual(data["autostart"]["items"][0]["running_mb"], 64.0, places=1)

    def test_shared_is_in_the_json(self):
        entries = [entry("A", r"C:\a.exe"), entry("B", r"C:\a.exe")]
        data = build(entries, [fake_proc(10, r"C:\a.exe", 10.0)]).as_dict()
        self.assertEqual(data["autostart"]["items"][0]["shared"], 2)


class TestJsonFiltering(unittest.TestCase):
    """`--json --kind X` 必须真的只给 X。

    第一版 JSON 路径完全忽略了 `only` / `limit`，每种 kind 都吐出全部 183 条 ——
    脚本按 kind 过滤时拿到的是错数据，而且看起来一切正常。
    """

    def setUp(self):
        self.entries = [
            entry("RunA", r"C:\a.exe", kind="run"),
            entry("TaskA", r"C:\t.exe", kind="task"),
            entry("TaskB", r"C:\t2.exe", kind="task"),
            entry("SvcA", r"C:\s.exe", kind="service"),
        ]
        self.rep = build(
            self.entries,
            [fake_proc(1, r"C:\a.exe", 10.0), fake_proc(2, r"C:\t.exe", 20.0)],
        )

    def test_only_keeps_one_kind(self):
        data = self.rep.as_dict(only="task")
        self.assertEqual([i["name"] for i in data["autostart"]["items"]], ["TaskA", "TaskB"])
        self.assertEqual(data["autostart"]["total"], 2)

    def test_counts_are_recomputed_after_filtering(self):
        data = self.rep.as_dict(only="task")
        self.assertEqual(data["autostart"]["total"], len(data["autostart"]["items"]))
        self.assertEqual(
            data["autostart"]["enabled"] + data["autostart"]["disabled"],
            data["autostart"]["total"],
        )

    def test_limit_applies(self):
        data = self.rep.as_dict(limit=2)
        self.assertEqual(len(data["autostart"]["items"]), 2)
        self.assertEqual(data["autostart"]["total"], 2)

    def test_only_and_limit_together(self):
        data = self.rep.as_dict(only="task", limit=1)
        self.assertEqual([i["name"] for i in data["autostart"]["items"]], ["TaskA"])

    def test_no_filter_is_everything(self):
        data = self.rep.as_dict()
        self.assertEqual(data["autostart"]["total"], len(self.entries))

    def test_memory_follows_the_filter(self):
        full = self.rep.as_dict()
        only_run = self.rep.as_dict(only="run")
        self.assertGreater(full["autostart"]["memory_mb"], only_run["autostart"]["memory_mb"])
        self.assertAlmostEqual(only_run["autostart"]["memory_mb"], 10.0, places=1)

    def test_unknown_kind_gives_empty_not_everything(self):
        data = self.rep.as_dict(only="nope")
        self.assertEqual(data["autostart"]["total"], 0)
        self.assertEqual(data["autostart"]["items"], [])


class TestBackgroundGrouping(unittest.TestCase):
    """「后台占内存」必须按程序合并。

    第一版直接列进程，同一个程序开 6 个进程就出 6 行同名条目 —— 看起来像重复
    bug，而且用户真正想知道的是「它一共占多少」。
    """

    def test_same_program_appears_once_with_a_count(self):
        processes = [
            fake_proc(1, r"C:\x\bg.exe", 300.0),
            fake_proc(2, r"C:\x\bg.exe", 200.0),
        ]
        # 默认 Windows() 就是没有可见窗口
        rep = build([], processes)
        self.assertEqual(len(rep.background), 1)
        self.assertEqual(rep.background[0].name, "bg.exe")
        self.assertEqual(len(rep.background[0].pids), 2)
        self.assertAlmostEqual(rep.background[0].mb, 500.0, places=1)

    def test_json_has_count_not_pid(self):
        rep = build([], [fake_proc(1, r"C:\x\bg.exe", 300.0)])
        data = rep.as_dict()["background"][0]
        self.assertEqual(data["count"], 1)
        self.assertNotIn("pid", data)
        self.assertAlmostEqual(data["mb"], 300.0, places=1)

    def test_small_processes_are_filtered_out(self):
        rep = build([], [fake_proc(1, r"C:\x\small.exe", 5.0)])
        self.assertEqual(rep.background, [])

    def test_system_hosts_are_not_listed_as_background_hogs(self):
        rep = build([], [fake_proc(1, r"C:\Windows\System32\svchost.exe", 400.0)])
        self.assertEqual(rep.background, [])

    def test_visible_window_excludes_a_process(self):
        proc = fake_proc(1, r"C:\x\fg.exe", 400.0)
        proc.windows = winapi.Windows(total=1, visible=1)
        rep = build([], [proc])
        self.assertEqual(rep.background, [])


class TestEmptyMachine(unittest.TestCase):
    """空机器（0 条自启、0 个进程）不能崩，也不能编数据。"""

    def setUp(self):
        self.rep = build([], [])
        # 这个文件里的 build() 把 boot_time 打成 0 了，这里给个固定值，
        # 否则「开机时间未知 + 登录时间未知」两行都不出现，测了个寂寞
        self.rep.boot = 1794390000.0
        self.lines = render(self.rep)

    def test_does_not_crash(self):
        self.assertTrue(self.lines)

    def test_unknown_logon_is_not_faked_as_boot_time(self):
        # 以前会退回开机时间，界面打出「开机 03:54　登录 03:54」——
        # 看起来是精确测量，其实是同一个数抄了两遍。
        text = "\n".join(self.lines)
        self.assertIn("登录时间未知", text)
        self.assertEqual(self.rep.logon, 0.0)

    def test_no_zero_process_note(self):
        # 「不是管理员：0 个进程读不到路径」纯属噪音
        text = "\n".join(self.lines)
        self.assertNotIn("0 个进程读不到", text)

    def test_no_double_blank_lines(self):
        for prev, cur in zip(self.lines, self.lines[1:]):
            self.assertFalse(prev == "" and cur == "", "出现了连续两个空行")

    def test_json_is_serialisable(self):
        data = self.rep.as_dict()
        self.assertEqual(data["autostart"]["total"], 0)
        self.assertEqual(data["autostart"]["items"], [])
        self.assertEqual(data["background"], [])


class TestHeaderMatchesWhatWasScanned(unittest.TestCase):
    """标题不能写没扫的来源。

    `--include registry` 只扫了注册表，标题还写「注册表 + 启动文件夹」就是在
    暗示启动文件夹也查过了、只是没有条目。
    """

    def _entry(self, kind, name):
        return autostart.Entry(name=name, kind=kind, source="x", scope="user")

    def test_registry_only(self):
        rep = build([self._entry("run", "a"), self._entry("runonce", "b")], [])
        self.assertIn("开机自启（注册表）", "\n".join(render(rep)))

    def test_folder_only(self):
        rep = build([self._entry("folder", "a.lnk")], [])
        self.assertIn("开机自启（启动文件夹）", "\n".join(render(rep)))

    def test_mixed_keeps_the_combined_label(self):
        rep = build([self._entry("run", "a"), self._entry("folder", "b.lnk")], [])
        self.assertIn("开机自启（注册表 + 启动文件夹）", "\n".join(render(rep)))


class TestCaveatSurfacing(unittest.TestCase):
    """扫描器「没查清楚」的话必须出现在表格里。

    以前这些只写进 JSON 的 `note` 字段，命令行表格一个字都不显示 —— 用户看到
    光秃秃一个文件名，以为一切正常。
    """

    def _entry(self, **kw):
        return autostart.Entry(
            name=kw.pop("name", "x.lnk"),
            kind=kw.pop("kind", "folder"),
            source=kw.pop("source", "用户启动夹"),
            scope="user",
            **kw,
        )

    def test_caveat_shows_in_flags(self):
        item = report.Item(self._entry(caveat="快捷方式的目标没解出来，只能看到文件名"))
        self.assertIn("快捷方式的目标没解出来，只能看到文件名", item.flags)

    def test_caveat_shows_in_the_table(self):
        rep = build([self._entry(caveat="快捷方式的目标没解出来，只能看到文件名")], [])
        self.assertIn("快捷方式的目标没解出来", "\n".join(render(rep)))

    def test_plain_state_note_does_not_pollute_the_column(self):
        # 「正在运行」这类状态描述 114 条服务都有，塞进备注列就是噪音
        rep = build([self._entry(kind="service", source="服务", note="正在运行")], [])
        self.assertNotIn("正在运行", "\n".join(render(rep)))

    def test_caveat_is_in_the_json(self):
        data = build([self._entry(caveat="读不到可执行文件路径，按系统组件处理")], []).as_dict()
        item = data["autostart"]["items"][0]
        self.assertEqual(item["caveat"], "读不到可执行文件路径，按系统组件处理")
        self.assertIn("读不到可执行文件路径，按系统组件处理", item["flags"])


class TestGroupCoverage(unittest.TestCase):
    def test_every_process_lands_in_exactly_one_group(self):
        procs_list = [
            fake_proc(1, r"C:\a.exe", 10.0),
            fake_proc(2, r"C:\a.exe", 20.0),
            fake_proc(3, r"C:\b.exe", 30.0),
        ]
        groups = procs.group(procs_list)
        pids = [pid for g in groups for pid in g.pids]
        self.assertEqual(sorted(pids), [1, 2, 3])
        self.assertEqual(len(pids), len(set(pids)), "同一个进程出现在两个组里")
        self.assertAlmostEqual(sum(g.mb for g in groups), 60.0, places=1)


if __name__ == "__main__":
    unittest.main()
