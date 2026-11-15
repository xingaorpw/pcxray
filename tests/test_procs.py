"""进程分组的逻辑测试。用假的 Process 对象，不需要真的跑在 Windows 上。"""

from __future__ import annotations

import time
import unittest

from pcxray import procs, winapi


def fake(
    pid: int,
    name: str,
    mb: float = 0.0,
    path: str = "",
    visible: bool = False,
    started: float = 0.0,
    readable: bool = True,
) -> winapi.Process:
    windows = winapi.Windows(total=1 if visible else 0, visible=1 if visible else 0)
    return winapi.Process(
        pid=pid,
        name=name,
        path=path,
        working_set=int(mb * 1048576),
        private=int(mb * 1048576),
        started=started,
        windows=windows,
        readable=readable,
    )


class TestGroup(unittest.TestCase):
    def test_merges_same_name(self):
        groups = procs.group([fake(1, "chrome.exe", 100), fake(2, "chrome.exe", 50)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].mb, 150)
        self.assertEqual(len(groups[0].pids), 2)

    def test_sorted_by_memory(self):
        groups = procs.group([fake(1, "a.exe", 10), fake(2, "b.exe", 90)])
        self.assertEqual([g.name for g in groups], ["b.exe", "a.exe"])

    def test_case_insensitive_merge(self):
        groups = procs.group([fake(1, "App.exe", 10), fake(2, "app.exe", 20)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].mb, 30)

    def test_has_window_if_any_visible(self):
        groups = procs.group([fake(1, "x.exe", 10, visible=False), fake(2, "x.exe", 10, visible=True)])
        self.assertTrue(groups[0].has_window)

    def test_no_window_if_none_visible(self):
        groups = procs.group([fake(1, "x.exe", 10, visible=False)])
        self.assertFalse(groups[0].has_window)

    def test_started_is_earliest(self):
        groups = procs.group([fake(1, "x.exe", 10, started=200), fake(2, "x.exe", 10, started=100)])
        self.assertEqual(groups[0].started, 100)

    def test_as_dict_shape(self):
        data = procs.group([fake(1, "x.exe", 10, path="C:\\x.exe")])[0].as_dict()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["path"], "C:\\x.exe")
        self.assertIn("mb", data)


class TestLogonTime(unittest.TestCase):
    def test_uses_explorer(self):
        ps = [fake(1, "a.exe", started=500), fake(2, "explorer.exe", started=1000)]
        self.assertEqual(procs.logon_time(ps), 1000)

    def test_falls_back_to_earliest_readable(self):
        ps = [fake(1, "a.exe", started=700), fake(2, "b.exe", started=300)]
        self.assertEqual(procs.logon_time(ps), 300)

    def test_ignores_unreadable_when_falling_back(self):
        ps = [fake(1, "a.exe", started=700), fake(2, "b.exe", started=300, readable=False)]
        self.assertEqual(procs.logon_time(ps), 700)

    def test_empty_returns_unknown_not_boot_time(self):
        # 没有进程可读时，登录时间就是「不知道」。以前返回开机时间，界面上会
        # 打出「开机 03:54　登录 03:54」—— 看起来是精确测量，其实是编的。
        self.assertEqual(procs.logon_time([]), 0.0)

    def test_unreadable_only_returns_unknown(self):
        ps = [fake(1, "a.exe", started=700, readable=False)]
        self.assertEqual(procs.logon_time(ps), 0.0)


class TestPathHelpers(unittest.TestCase):
    """这两个函数原来用 os.path，在 Linux 上直接失效 —— CI 就是这么挂的。"""

    def test_base_of_windows_path_on_any_os(self):
        self.assertEqual(procs._win_base(r"C:\Windows\system32\cmd.exe"), "cmd.exe")

    def test_base_with_quotes(self):
        self.assertEqual(procs._win_base(r'"C:\a b\Thing.EXE"'), "thing.exe")

    def test_key_normalises_separators_and_case(self):
        self.assertEqual(procs._win_key(r"C:\Apps\X.EXE"), procs._win_key("c:/apps/x.exe"))

    def test_key_strips_trailing_separator(self):
        self.assertEqual(procs._win_key(r"C:\Apps\X.exe\ "), r"c:\apps\x.exe")

    def test_matching_survives_linux_style_os_path(self):
        """把 os.path 换成 Linux 的行为，匹配必须照旧。

        这就是 CI 在 ubuntu 上挂掉的那个场景：`normcase` 在 Linux 上是空操作，
        `basename` 不认 `\\`。如果代码退回去用 os.path，这条会立刻失败。
        """
        import os
        import posixpath
        from unittest import mock

        ps = [fake(1, "cmd.exe", path=r"C:\Windows\system32\cmd.exe")]
        with mock.patch.object(os.path, "normcase", posixpath.normcase), mock.patch.object(
            os.path, "basename", posixpath.basename
        ):
            # 宿主程序仍要被排除
            self.assertEqual(procs.running_of(r"C:\Windows\system32\cmd.exe", ps), [])
            # 大小写不同的同一条路径仍要匹配上
            ps2 = [fake(2, "x.exe", path=r"C:\Apps\X.EXE")]
            self.assertEqual(len(procs.running_of(r"c:\apps\x.exe", ps2)), 1)


class TestRunningOf(unittest.TestCase):
    def test_exact_path_match(self):
        ps = [fake(1, "x.exe", path=r"C:\Program Files\X\x.exe")]
        self.assertEqual(len(procs.running_of(r"C:\Program Files\X\x.exe", ps)), 1)

    def test_case_insensitive(self):
        ps = [fake(1, "x.exe", path=r"C:\Apps\X.EXE")]
        self.assertEqual(len(procs.running_of(r"c:\apps\x.exe", ps)), 1)

    def test_different_path_no_match(self):
        ps = [fake(1, "x.exe", path=r"C:\Other\x.exe")]
        self.assertEqual(procs.running_of(r"C:\Apps\x.exe", ps), [])

    def test_empty_exe(self):
        self.assertEqual(procs.running_of("", [fake(1, "x.exe", path="C:\\x.exe")]), [])

    def test_shared_host_not_attributed(self):
        # svchost 被 100 多个服务共用，归属给某一条会得出荒唐数字
        ps = [fake(1, "svchost.exe", 400, path=r"C:\Windows\system32\svchost.exe")]
        self.assertEqual(procs.running_of(r"C:\Windows\system32\svchost.exe", ps), [])

    def test_cmd_not_attributed(self):
        ps = [fake(1, "cmd.exe", 1, path=r"C:\Windows\system32\cmd.exe")]
        self.assertEqual(procs.running_of(r"C:\Windows\system32\cmd.exe", ps), [])

    def test_normal_app_still_attributed(self):
        ps = [fake(1, "clash-verge.exe", 20, path=r"C:\Program Files\Clash Verge\clash-verge.exe")]
        self.assertEqual(len(procs.running_of(r"C:\Program Files\Clash Verge\clash-verge.exe", ps)), 1)


class TestAutostartPids(unittest.TestCase):
    def test_within_window(self):
        ps = [fake(1, "a.exe", started=1050)]
        self.assertEqual(procs.autostart_pids(ps, logon=1000), {1})

    def test_outside_window(self):
        ps = [fake(1, "a.exe", started=1000 + procs.LOGON_WINDOW + 1)]
        self.assertEqual(procs.autostart_pids(ps, logon=1000), set())

    def test_started_before_logon_excluded(self):
        ps = [fake(1, "a.exe", started=900)]
        self.assertEqual(procs.autostart_pids(ps, logon=1000), set())

    def test_no_logon_returns_empty(self):
        self.assertEqual(procs.autostart_pids([fake(1, "a.exe", started=5)], logon=0), set())


class TestBackgroundHeavy(unittest.TestCase):
    def test_hidden_and_big(self):
        ps = [fake(1, "x.exe", 200, visible=False)]
        self.assertEqual(len(procs.background_heavy(ps)), 1)

    def test_small_excluded(self):
        ps = [fake(1, "x.exe", 50, visible=False)]
        self.assertEqual(procs.background_heavy(ps), [])

    def test_visible_excluded(self):
        ps = [fake(1, "x.exe", 500, visible=True)]
        self.assertEqual(procs.background_heavy(ps), [])

    def test_system_processes_excluded(self):
        ps = [fake(1, "lsass.exe", 500, visible=False), fake(2, "svchost.exe", 500, visible=False)]
        self.assertEqual(procs.background_heavy(ps), [])

    def test_sorted_desc(self):
        ps = [fake(1, "a.exe", 150, visible=False), fake(2, "b.exe", 300, visible=False)]
        self.assertEqual([p.name for p in procs.background_heavy(ps)], ["b.exe", "a.exe"])

    def test_custom_threshold(self):
        ps = [fake(1, "x.exe", 60, visible=False)]
        self.assertEqual(len(procs.background_heavy(ps, min_mb=50)), 1)


class TestDescribeStarted(unittest.TestCase):
    def test_at_logon(self):
        self.assertIn("随登录启动", procs.describe_started(1000, 1000))

    def test_before_logon(self):
        self.assertIn("登录前", procs.describe_started(900, 1000))

    def test_after_window(self):
        text = procs.describe_started(1000 + 5000, 1000)
        self.assertIn("启动", text)
        self.assertNotIn("随登录", text)

    def test_unknown(self):
        self.assertEqual(procs.describe_started(0, 1000), "启动时间未知")


class TestWindowsFlag(unittest.TestCase):
    def test_hidden_when_no_visible(self):
        self.assertTrue(winapi.Windows(total=5, visible=0).hidden)

    def test_not_hidden_when_visible(self):
        self.assertFalse(winapi.Windows(total=5, visible=1).hidden)


class TestFutureStartTimeIsCalledOut(unittest.TestCase):
    """系统时间被往回调过时，进程的启动时间会「在未来」。

    直接显示一个未来时刻，用户会当成正常启动时间 —— 得说清楚。
    """

    def test_future_timestamp_is_flagged(self):
        now = time.time()
        text = procs.describe_started(now + 3600, 0)
        self.assertIn("在未来", text)

    def test_normal_timestamp_is_not_flagged(self):
        now = time.time()
        self.assertNotIn("在未来", procs.describe_started(now - 60, 0))

    def test_unknown_stays_unknown(self):
        self.assertEqual(procs.describe_started(0, 0), "启动时间未知")


if __name__ == "__main__":
    unittest.main()
