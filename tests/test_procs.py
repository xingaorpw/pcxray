"""进程分组的逻辑测试。用假的 Process 对象，不需要真的跑在 Windows 上。"""

from __future__ import annotations

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

    def test_empty_returns_boot_time(self):
        # 没有进程时退回开机时间。两次调用会差几毫秒，所以给 1 秒容差。
        self.assertAlmostEqual(procs.logon_time([]), winapi.boot_time(), delta=1.0)


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


if __name__ == "__main__":
    unittest.main()
