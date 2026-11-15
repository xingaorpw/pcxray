"""自启扫描里那些不依赖 Windows 的逻辑：命令行解析、开关状态解码。"""

from __future__ import annotations

import os
import tempfile
import contextlib
import unittest
from unittest import mock

from pcxray import autostart


class TestDecodeApproved(unittest.TestCase):
    """`StartupApproved` 的编码是实测出来的，这里把实测值钉死。"""

    def test_enabled_02(self):
        self.assertTrue(autostart.decode_approved(bytes.fromhex("020000000000000000000000")))

    def test_disabled_04_real_value(self):
        # 本机 HKLM 里 SecurityHealth / LVAPRCU / preinsassoguard 的真实字节
        self.assertFalse(autostart.decode_approved(bytes.fromhex("040000000000000000000000")))

    def test_disabled_03(self):
        self.assertFalse(autostart.decode_approved(bytes.fromhex("03000000aabbccddeeff0011")))

    def test_enabled_06(self):
        self.assertTrue(autostart.decode_approved(bytes.fromhex("060000000000000000000000")))

    def test_empty_bytes_is_unknown(self):
        self.assertIsNone(autostart.decode_approved(b""))

    def test_string_value_is_unknown(self):
        # 本机 HKCU 真有一条 REG_SZ 的空值（GalaxyClient），不能当成禁用
        self.assertIsNone(autostart.decode_approved(""))

    def test_none_is_unknown(self):
        self.assertIsNone(autostart.decode_approved(None))

    def test_what_we_write_is_what_we_can_read_back(self):
        """工具写进去的标记，自己必须能读回来。

        这条看着废话，但它防的是一个真事故：写入用一套编码、读取用另一套，
        于是「关掉」之后界面还显示「启用」，用户会以为没生效然后反复点。
        """
        from pcxray import actions

        self.assertTrue(autostart.decode_approved(actions._ENABLED))
        self.assertFalse(autostart.decode_approved(actions._disabled_now()))

    def test_written_disabled_flag_is_the_task_manager_form(self):
        # 任务管理器自己写的是 03 + 8 字节时间戳（禁用时刻）。别写 04 + 零：
        # 那种形式是别的工具写的，也能用，但跟我们声称「等于在任务管理器里点禁用」不符。
        from pcxray import actions

        flag = actions._disabled_now()
        self.assertEqual(len(flag), 12)
        self.assertEqual(flag[0], 0x03)
        self.assertEqual(actions._ENABLED[0], 0x02)
        self.assertEqual(len(actions._ENABLED), 12)

    def test_one_byte_enough(self):
        self.assertTrue(autostart.decode_approved(b"\x02"))

    def test_bytearray_accepted(self):
        self.assertFalse(autostart.decode_approved(bytearray(b"\x04\x00")))


class TestExpandVars(unittest.TestCase):
    def test_expands_known(self):
        os.environ["PCXRAY_TEST_VAR"] = "hello"
        try:
            self.assertEqual(autostart.expand_vars("%PCXRAY_TEST_VAR%\\x.exe"), "hello\\x.exe")
        finally:
            del os.environ["PCXRAY_TEST_VAR"]

    def test_keeps_unknown(self):
        self.assertEqual(autostart.expand_vars("%NO_SUCH_VAR_XYZ%\\x.exe"), "%NO_SUCH_VAR_XYZ%\\x.exe")

    def test_empty(self):
        self.assertEqual(autostart.expand_vars(""), "")


class TestSplitExe(unittest.TestCase):
    def test_quoted_path_with_spaces(self):
        cmd = '"C:\\Program Files\\Epic Games\\Launcher\\EpicGamesLauncher.exe" -silent'
        self.assertEqual(autostart.split_exe(cmd), "C:\\Program Files\\Epic Games\\Launcher\\EpicGamesLauncher.exe")

    def test_quoted_no_args(self):
        self.assertEqual(autostart.split_exe('"C:\\a b\\c.exe"'), "C:\\a b\\c.exe")

    def test_unquoted_single_token(self):
        self.assertEqual(autostart.split_exe("C:\\Windows\\system32\\ctfmon.exe"), "C:\\Windows\\system32\\ctfmon.exe")

    def test_unbalanced_quote_does_not_crash(self):
        self.assertEqual(autostart.split_exe('"C:\\a b\\c.exe'), "C:\\a b\\c.exe")

    def test_existing_file_with_spaces_unquoted(self):
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "my app.exe")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("x")
            self.assertEqual(autostart.split_exe(target + " --flag"), target)

    def test_rundll32(self):
        cmd = "rundll32.exe C:\\Windows\\system32\\shell32.dll,OpenAs_RunDLL"
        self.assertEqual(autostart.split_exe(cmd).lower(), "rundll32.exe")

    def test_empty(self):
        self.assertEqual(autostart.split_exe(""), "")

    def test_expand_then_split(self):
        os.environ["PCXRAY_DIR"] = "C:\\Windows\\system32"
        try:
            self.assertEqual(
                autostart.split_exe("%PCXRAY_DIR%\\notepad.exe"), "C:\\Windows\\system32\\notepad.exe"
            )
        finally:
            del os.environ["PCXRAY_DIR"]


class TestFolderScanEdges(unittest.TestCase):
    """启动文件夹和 .lnk 解析的边界。"""

    def test_resolve_links_with_no_paths_does_not_call_powershell(self):
        with mock.patch.object(autostart.pwsh, "run_json") as run:
            self.assertEqual(autostart._resolve_links([]), {})
        run.assert_not_called()

    def test_resolve_links_failure_returns_empty_dict(self):
        # 解不出目标不是错误，整个启动文件夹照常列出来
        with mock.patch.object(autostart.pwsh, "run_json", return_value=(False, None, "超时")):
            self.assertEqual(autostart._resolve_links([r"C:\a.lnk"]), {})

    def test_resolve_links_skips_non_dict_rows(self):
        with mock.patch.object(
            autostart.pwsh, "run_json", return_value=(True, ["垃圾", {"Path": "C:\\A.LNK", "Target": "C:\\a.exe"}], "")
        ):
            got = autostart._resolve_links([r"C:\a.lnk"])
        self.assertEqual(got, {r"c:\a.lnk": (r"C:\a.exe", "")})

    def test_missing_startup_folder_is_skipped(self):
        with mock.patch.object(
            autostart, "_startup_dirs", return_value=[("user", r"Z:\不存在的目录", "用户启动夹")]
        ):
            self.assertEqual(autostart.scan_folders(), [])

    def test_unreadable_startup_folder_is_skipped(self):
        with mock.patch.object(
            autostart, "_startup_dirs", return_value=[("user", r"Z:\x", "用户启动夹")]
        ), mock.patch.object(autostart.os.path, "isdir", return_value=True), mock.patch.object(
            autostart.os, "listdir", side_effect=PermissionError("拒绝访问")
        ):
            self.assertEqual(autostart.scan_folders(), [])

    def test_plain_file_in_startup_folder_is_listed(self):
        # 启动文件夹里不只有 .lnk，.bat / .exe 也会被直接启动
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "run.bat"), "w", encoding="utf-8") as handle:
                handle.write("echo hi")
            with mock.patch.object(
                autostart, "_startup_dirs", return_value=[("user", tmp, "用户启动夹")]
            ), mock.patch.object(autostart, "approved_state", return_value="enabled"):
                entries = autostart.scan_folders()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].command.endswith("run.bat"), entries[0].command)
        self.assertEqual(entries[0].exe, entries[0].command)


class TestDisabledKeyNonStringValue(unittest.TestCase):
    """被挪到 -pcxray-disabled 的键里如果有非字符串值，要报出来而不是悄悄跳过。"""

    def test_non_string_value_is_reported(self):
        def fake_read(hive, subkey):
            if subkey.endswith(autostart.DISABLED_SUFFIX):
                return [("X", 123, 4)]
            return []

        problems: list[str] = []
        with mock.patch.object(autostart, "_read_values", side_effect=fake_read):
            autostart.scan_registry(problems)
        # 每个注册表位置都会报一条（HKCU/HKLM 的 Run、Run32、RunOnce…）
        self.assertTrue(problems, "非字符串值必须报出来，不能悄悄跳过")
        self.assertTrue(all("不是字符串" in p for p in problems), problems)


class TestComHandlerTaskCaveat(unittest.TestCase):
    """46 个登录/开机任务里有 22 个没有命令行 —— 它们靠 COM 处理器启动。

    空白的「程序」一栏看起来像「这个任务什么都不干」，恰恰相反。
    """

    ROWS = [
        {"Name": "NoExe", "Path": "\\Custom\\", "State": 3, "Triggers": "Logon",
         "Exe": "", "Args": ""},
        {"Name": "WithExe", "Path": "\\Custom\\", "State": 3, "Triggers": "Logon",
         "Exe": r"C:\Windows\notepad.exe", "Args": ""},
    ]

    def test_task_without_execute_gets_a_caveat(self):
        with mock.patch.object(autostart.pwsh, "run_json", return_value=(True, self.ROWS, "")):
            entries, _reason = autostart.scan_tasks()
        by_name = {e.name: e for e in entries}
        self.assertIn("COM", by_name["NoExe"].caveat)
        self.assertEqual(by_name["WithExe"].caveat, "")

    def test_caveat_fits_the_table_column(self):
        # 备注列放不下会被截成「没有命令行（靠 COM …」，最要紧的后半句就没了
        with mock.patch.object(autostart.pwsh, "run_json", return_value=(True, self.ROWS, "")):
            entries, _reason = autostart.scan_tasks()
        caveat = next(e for e in entries if e.name == "NoExe").caveat
        self.assertLessEqual(len(caveat), 16, "再长就会被截断，换个短说法")


class TestUnknownIncludeIsReported(unittest.TestCase):
    """传错来源名（比如单数 `service`）以前会安安静静返回 0 条。

    调用方会以为「这一类真的没有东西」—— 空列表和「没扫」看起来一样。
    """

    def test_singular_name_is_reported(self):
        with mock.patch.object(autostart, "scan_registry", return_value=[]), mock.patch.object(
            autostart, "scan_folders", return_value=[]
        ):
            entries, problems = autostart.scan(("service",))
        self.assertEqual(entries, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("service", problems[0])
        self.assertIn("不认识的来源", problems[0])

    def test_known_names_produce_no_complaint(self):
        with mock.patch.object(autostart, "scan_registry", return_value=[]):
            _entries, problems = autostart.scan(("registry",))
        self.assertEqual(problems, [])


class TestLegacyMoveLeftovers(unittest.TestCase):
    """旧版 `--mode move` 在同一个键里把值改名成 `~pcxray~原名`。

    那拦不住启动（Run 里每个值 Windows 都会执行），所以这种条目其实一直开着。
    名字骗人，状态不能跟着骗 —— 报「启用」并说清楚怎么处理。
    """

    def _scan(self, values):
        def fake_read(_hive, subkey):
            if subkey.endswith(autostart.DISABLED_SUFFIX):
                return []
            return values

        with mock.patch.object(
            autostart, "RUN_KEYS", (("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", "user", "run", "用户 Run"),)
        ), mock.patch.object(autostart, "_read_values", side_effect=fake_read), mock.patch.object(
            autostart, "approved_state", return_value=None
        ):
            return autostart.scan_registry()

    def test_legacy_name_is_reported_as_still_enabled(self):
        entries = self._scan([("~pcxray~旧条目", r"C:\x\old.exe", 1)])
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].enabled, "值还在 Run 里，Windows 就会启动它")
        self.assertIn("旧版 --mode move", entries[0].caveat)
        self.assertIn("一直开着", entries[0].caveat)

    def test_normal_name_gets_no_such_caveat(self):
        entries = self._scan([("正常条目", r"C:\x\a.exe", 1)])
        self.assertEqual(entries[0].caveat, "")


class TestEntry(unittest.TestCase):
    def test_key_is_stable_and_unique(self):
        a = autostart.Entry(name="OneDrive", kind="run", source="s", scope="user")
        b = autostart.Entry(name="OneDrive", kind="run", source="s", scope="user")
        c = autostart.Entry(name="OneDrive", kind="task", source="s", scope="user")
        self.assertEqual(a.key, b.key)
        self.assertNotEqual(a.key, c.key)

    def test_key_ignores_command_change(self):
        # 命令变了但名字没变 → 还是同一条，界面上不会「变回启用」
        a = autostart.Entry(name="X", kind="run", source="s", command="a.exe")
        b = autostart.Entry(name="X", kind="run", source="s", command="b.exe")
        self.assertEqual(a.key, b.key)

    def test_as_dict_roundtrip(self):
        e = autostart.Entry(name="X", kind="run", source="s", locator={"hive": "HKCU"})
        data = e.as_dict()
        self.assertEqual(data["name"], "X")
        self.assertEqual(data["locator"]["hive"], "HKCU")

    def test_repr_mentions_state(self):
        e = autostart.Entry(name="X", kind="run", source="s", enabled=False)
        self.assertIn("禁用", repr(e))


class TestEveryRunKeyViewIsScanned(unittest.TestCase):
    """四个「视图」× 两种键，一个都不能漏。

    Windows 的注册表重定向让 32 位程序看到的是 `WOW6432Node` 那套键。当前用户的
    那份写它不需要管理员 —— 32 位程序装给自己用的自启项、以及一些恶意程序喜欢
    藏的地方都在那儿。以前只扫了 HKLM 的 32 位视图，HKCU 的整份漏掉：实测往
    `HKCU\\Software\\WOW6432Node\\...\\Run` 写一个值，`pcxray list` 一条都看不到。
    """

    def test_all_eight_views_are_present(self):
        found = {(hive, subkey) for hive, subkey, _scope, _kind, _label in autostart.RUN_KEYS}
        expected = set()
        for hive in ("HKCU", "HKLM"):
            for middle in ("Software", r"Software\WOW6432Node"):
                for leaf in ("Run", "RunOnce"):
                    expected.add((hive, middle + "\\Microsoft\\Windows\\CurrentVersion\\" + leaf))
        self.assertEqual(expected - found, set(), "这些位置没扫")
        self.assertEqual(found - expected, set(), "多了不该扫的位置")

    def test_32bit_entries_use_the_run32_approval_column(self):
        for hive, subkey, _scope, kind, _label in autostart.RUN_KEYS:
            if "WOW6432Node" in subkey:
                self.assertEqual(kind, "run32" if subkey.endswith("\\Run") else "runonce", subkey)


class TestWeirdRegistryValueTypes(unittest.TestCase):
    """Run 里的值不一定是字符串。

    有些程序（和一部分恶意软件）会把 Run 的值写成 REG_DWORD。Windows 不会拿它
    启动任何东西，但它确实在注册表里 —— 静默跳过等于让用户以为「这一条不存在」。
    """

    SINGLE_KEY = (
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", "user", "run", "用户 Run"),
    )

    def setUp(self):
        self.problems: list[str] = []

    @contextlib.contextmanager
    def _fake_values(self, values):
        # 只留一个键，不然同一个假值会被 5 个键各读一遍
        def fake(_hive, subkey):
            # 被 --mode move 挪走的键里没有东西 —— 假数据只喂给 Run 本身
            if subkey.endswith(autostart.DISABLED_SUFFIX):
                return []
            return values

        with mock.patch.object(autostart, "RUN_KEYS", self.SINGLE_KEY), mock.patch.object(
            autostart, "_read_values", side_effect=fake
        ):
            yield

    def test_non_string_value_is_reported_not_swallowed(self):
        with self._fake_values([("BadType", 12345, 4)]):
            entries = autostart.scan_registry(self.problems)
        self.assertEqual(entries, [])
        self.assertEqual(len(self.problems), 1)
        self.assertIn("BadType", self.problems[0])
        self.assertIn("不是字符串", self.problems[0])

    def test_no_problems_list_still_works(self):
        # 老调用方式（不传 problems）不能炸
        with self._fake_values(
            [("BadType", 12345, 4), ("Good", r'"C:\Windows\notepad.exe"', 1)]
        ):
            entries = autostart.scan_registry()
        self.assertEqual([e.name for e in entries], ["Good"])

    def test_empty_string_value_gets_a_caveat(self):
        with self._fake_values([("Empty", "", 1)]):
            entries = autostart.scan_registry(self.problems)
        self.assertEqual(len(entries), 1)
        self.assertIn("空的", entries[0].caveat)

    def test_normal_value_has_no_caveat(self):
        with self._fake_values([("Good", r'"C:\Windows\notepad.exe"', 1)]):
            entries = autostart.scan_registry(self.problems)
        self.assertEqual(entries[0].caveat, "")
        self.assertEqual(self.problems, [])


if __name__ == "__main__":
    unittest.main()
