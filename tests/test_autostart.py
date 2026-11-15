"""自启扫描里那些不依赖 Windows 的逻辑：命令行解析、开关状态解码。"""

from __future__ import annotations

import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
