"""安全判断测试。这套判断决定了「什么能关」，错一条就可能让用户开不了机。"""

from __future__ import annotations

import unittest

from pcxray import safety


class TestProtectedProcess(unittest.TestCase):
    def test_system_is_protected(self):
        self.assertTrue(safety.protected_process("System"))

    def test_case_insensitive(self):
        self.assertTrue(safety.protected_process("LSASS.EXE"))

    def test_svchost_protected(self):
        self.assertTrue(safety.protected_process("svchost.exe"))

    def test_normal_app_not_protected(self):
        self.assertFalse(safety.protected_process("Weixin.exe"))

    def test_empty(self):
        self.assertFalse(safety.protected_process(""))


class TestProtectedService(unittest.TestCase):
    def test_defender_family(self):
        self.assertTrue(safety.protected_service("WinDefend"))
        self.assertTrue(safety.protected_service("SecurityHealthService"))

    def test_firewall_and_dns(self):
        self.assertTrue(safety.protected_service("mpssvc"))
        self.assertTrue(safety.protected_service("Dnscache"))

    def test_third_party_allowed(self):
        self.assertFalse(safety.protected_service("AdskLicensingService"))
        self.assertFalse(safety.protected_service("365VPNDaemon"))

    def test_short_name_does_not_prefix_match(self):
        # 第一版用前缀正则，"audio" 会把 AudioDeviceVendorTool 也保护掉
        self.assertFalse(safety.protected_service("AudioDeviceVendorTool"))

    def test_exe_in_system32_is_protected(self):
        # 光看名字认不全微软的服务，所以再加一条：exe 在 System32 下就算系统组件
        self.assertTrue(
            safety.protected_service("SomeNewMicrosoftSvc", r"C:\Windows\System32\svchost.exe -k netsvcs")
        )

    def test_exe_in_driverstore_is_protected(self):
        self.assertTrue(
            safety.protected_service(
                "RtkAudUService",
                r'"C:\Windows\System32\DriverStore\FileRepository\realtekservice.inf_amd64_x\RtkAudUService64.exe"',
            )
        )

    def test_exe_in_program_files_not_protected(self):
        self.assertFalse(
            safety.protected_service("ClashVergeSvc", r'"C:\Program Files\Clash Verge\clash-verge-service.exe"')
        )

    def test_bare_svchost_is_protected(self):
        # 有的服务 PathName 只有 `svchost.exe -k netsvcs`，没有目录部分。
        # 光看路径会漏判，所以要按 exe 名字再兜一层。
        self.assertTrue(safety.protected_service("SomeSvc", "svchost.exe -k netsvcs -p"))

    def test_quoted_svchost_is_protected(self):
        self.assertTrue(
            safety.protected_service("SomeSvc", r'"C:\Windows\System32\svchost.exe" -k netsvcs')
        )

    def test_whole_command_line_wrapped_in_quotes(self):
        # 有的服务把整条命令行用引号包起来，路径和参数一起在引号里。
        # 这时不能只取引号内容当路径，否则会得到 `svchost.exe -k netsvcs`。
        self.assertTrue(
            safety.protected_service("SomeSvc", '"C:\\Windows\\system32\\svchost.exe -k netsvcs"')
        )

    def test_name_containing_svchost_is_not_a_system_host(self):
        # `mysvchost.exe` 只是名字里带 svchost，不是 Windows 的宿主。
        # 之前为了过测试加过「命令行里出现 svchost.exe 子串就算系统组件」，
        # 那条会把这种第三方程序也保护掉 —— 是错的方向，删了。
        self.assertFalse(
            safety.protected_service("VendorSvc", r"C:\Program Files\Vendor\mysvchost.exe -run")
        )

    def test_name_only_does_not_guess(self):
        # 只给名字时不能替调用方假设「不知道就当系统组件」——那是扫描器的策略
        self.assertFalse(safety.protected_service("AdskLicensingService"))


class TestExeOf(unittest.TestCase):
    def test_quoted_with_args(self):
        self.assertEqual(safety.exe_of(r'"C:\a b\X.EXE" --flag'), "x.exe")

    def test_unquoted_with_args(self):
        self.assertEqual(safety.exe_of(r"C:\Windows\system32\ctfmon.exe -x"), "ctfmon.exe")

    def test_bare_name(self):
        self.assertEqual(safety.exe_of("svchost.exe -k netsvcs"), "svchost.exe")

    def test_empty(self):
        self.assertEqual(safety.exe_of(""), "")

    def test_forward_slashes(self):
        self.assertEqual(safety.exe_of("C:/Apps/Thing.exe"), "thing.exe")

    def test_whole_line_quoted_with_args(self):
        self.assertEqual(safety.exe_of('"C:\\Windows\\system32\\svchost.exe -k netsvcs"'), "svchost.exe")

    def test_quoted_path_with_space_and_args(self):
        # 引号只包路径、参数在外面：不能因为路径里有空格就切坏
        self.assertEqual(safety.exe_of('"C:\\Program Files\\X\\x.exe" --flag'), "x.exe")

    def test_name_with_svchost_substring_kept_whole(self):
        self.assertEqual(safety.exe_of(r"C:\Program Files\V\mysvchost.exe -run"), "mysvchost.exe")


class TestSuspectPath(unittest.TestCase):
    def test_temp(self):
        self.assertTrue(safety.suspect_path(r"C:\Users\a\AppData\Local\Temp\x.exe"))

    def test_windows_temp(self):
        self.assertTrue(safety.suspect_path(r"C:\Windows\Temp\x.exe"))

    def test_downloads(self):
        self.assertTrue(safety.suspect_path(r"C:\Users\a\Downloads\setup.exe"))

    def test_recycle_bin(self):
        self.assertTrue(safety.suspect_path(r"C:\$Recycle.Bin\S-1-5\x.exe"))

    def test_program_files_clean(self):
        self.assertEqual(safety.suspect_path(r"C:\Program Files\Clash Verge\clash-verge.exe"), "")

    def test_appdata_normal_clean(self):
        # 很多正常软件装在 AppData\Local\Programs 下，不能一律当可疑
        self.assertEqual(safety.suspect_path(r"C:\Users\a\AppData\Local\Programs\Notion\Notion.exe"), "")

    def test_empty(self):
        self.assertEqual(safety.suspect_path(""), "")

    def test_case_insensitive(self):
        self.assertTrue(safety.suspect_path(r"C:\USERS\A\APPDATA\LOCAL\TEMP\X.EXE"))

    def test_forward_slashes_normalized(self):
        self.assertTrue(safety.suspect_path("C:/Users/a/AppData/Local/Temp/x.exe"))


class TestHiddenArgs(unittest.TestCase):
    def test_silent(self):
        self.assertTrue(safety.hidden_args("x.exe --silent"))

    def test_background(self):
        self.assertTrue(safety.hidden_args("OneDrive.exe /background"))

    def test_windowstyle_hidden(self):
        self.assertTrue(safety.hidden_args("pwsh.exe -WindowStyle Hidden -c foo"))

    def test_no_startup_window(self):
        self.assertTrue(safety.hidden_args("msedge.exe --no-startup-window"))

    def test_plain_command(self):
        self.assertFalse(safety.hidden_args("C:\\Windows\\system32\\ctfmon.exe"))

    def test_empty(self):
        self.assertFalse(safety.hidden_args(""))

    # ---- 误报：以前是在整条命令行里找子串，路径和长得像的开关都会中招
    def test_directory_named_like_a_flag_is_not_a_flag(self):
        self.assertFalse(safety.hidden_args("C:\\tools\\--silent\\x.exe"))

    def test_background_color_is_not_hidden(self):
        self.assertFalse(safety.hidden_args("app.exe --background-color=red"))

    def test_backgroundtask_is_not_hidden(self):
        self.assertFalse(safety.hidden_args("app.exe /backgroundtask"))

    def test_path_containing_background_is_not_a_flag(self):
        self.assertFalse(safety.hidden_args("C:\\Program Files\\Background\\app.exe"))

    # ---- 漏报：同样因为只认固定几个整词
    def test_silent_start_counts(self):
        self.assertTrue(safety.hidden_args('"C:\\a\\clash.exe" --silent-start'))

    def test_minimized_counts(self):
        self.assertTrue(safety.hidden_args("app.exe -minimized"))

    def test_min_counts(self):
        self.assertTrue(safety.hidden_args("app.exe /min"))

    def test_hidden_flag_counts(self):
        self.assertTrue(safety.hidden_args("app.exe --hidden"))

    def test_whole_line_quoted_with_flag(self):
        self.assertTrue(safety.hidden_args('"C:\\Program Files\\App\\x.exe --silent"'))

    def test_flag_value_form(self):
        self.assertTrue(safety.hidden_args("app.exe --silent=true"))

    def test_unquoted_path_with_spaces_then_flag(self):
        self.assertTrue(safety.hidden_args("C:\\Program Files\\V\\x.exe --silent"))


class TestRefuseReason(unittest.TestCase):
    def test_runonce_refused(self):
        self.assertIn("一次性", safety.refuse_reason("runonce", "user", "x", False, True))

    def test_protected_refused(self):
        self.assertIn("系统自带", safety.refuse_reason("task", "user", "x", True, True))

    def test_service_not_hard_refused_for_admin(self):
        # 服务不是「绝不改」，而是「要再确认一次」—— 所以硬拒绝理由应该是空的
        self.assertEqual(safety.refuse_reason("service", "machine", "x", False, True), "")

    def test_service_needs_force(self):
        self.assertTrue(safety.needs_force("service"))

    def test_run_does_not_need_force(self):
        self.assertFalse(safety.needs_force("run"))
        self.assertFalse(safety.needs_force("task"))

    def test_machine_scope_needs_admin(self):
        self.assertIn("管理员", safety.refuse_reason("run", "machine", "x", False, False))

    def test_service_needs_admin_too(self):
        # 非管理员连服务都改不了，这个理由要排在「需确认」前面
        self.assertIn("管理员", safety.refuse_reason("service", "machine", "x", False, False))

    def test_user_run_is_allowed(self):
        self.assertEqual(safety.refuse_reason("run", "user", "x", False, False), "")

    def test_machine_run_allowed_when_admin(self):
        self.assertEqual(safety.refuse_reason("run", "machine", "x", False, True), "")

    def test_user_task_allowed(self):
        self.assertEqual(safety.refuse_reason("task", "user", "x", False, False), "")


if __name__ == "__main__":
    unittest.main()
