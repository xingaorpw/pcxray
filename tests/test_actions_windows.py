"""真的改注册表的端到端测试。只在 Windows 上跑。

这个文件是整个项目最重要的证据：它证明「关掉一条自启」这件事**真的做到了**，
而不是写了个看起来很对的函数。

做法：在 `HKCU\\...\\Run` 下建一条自己的测试值（指向 notepad.exe），关掉它，
读回 `StartupApproved` 的字节确认，再还原，最后清理干净。全程不碰用户已有的
任何一条自启项。
"""

from __future__ import annotations

import os
import sys
import unittest

from pcxray import actions, autostart, safety, winapi

WINDOWS = sys.platform == "win32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
TEST_NAME = "pcxray_selftest_do_not_use"


@unittest.skipUnless(WINDOWS, "只有 Windows 才有注册表 Run 键")
class TestRegistryRoundTrip(unittest.TestCase):
    def setUp(self):
        import winreg

        self.winreg = winreg
        # 先确保干净：万一上次跑崩了留下了残留
        self._delete_value(RUN_KEY, TEST_NAME)
        self._delete_value(APPROVED_KEY, TEST_NAME)
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(
                key, TEST_NAME, 0, winreg.REG_SZ, r"C:\Windows\system32\notepad.exe /test"
            )
        finally:
            key.Close()

    def tearDown(self):
        self._delete_value(RUN_KEY, TEST_NAME)
        self._delete_value(APPROVED_KEY, TEST_NAME)

    def _delete_value(self, subkey: str, name: str) -> None:
        import winreg

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE)
        except OSError:
            return
        try:
            winreg.DeleteValue(key, name)
        except OSError:
            pass
        finally:
            key.Close()

    def _read(self, subkey: str, name: str):
        import winreg

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey)
        except OSError:
            return None
        try:
            return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None
        finally:
            key.Close()

    def _entry(self) -> autostart.Entry:
        return autostart.Entry(
            name=TEST_NAME,
            kind="run",
            source="测试",
            scope="user",
            command=r"C:\Windows\system32\notepad.exe /test",
            exe=r"C:\Windows\system32\notepad.exe",
            enabled=True,
            locator={"hive": "HKCU", "subkey": RUN_KEY, "value": TEST_NAME},
        )

    # ---------------------------------------------------------------- 用例

    def test_disable_writes_approved_flag_and_reads_back(self):
        results, backup = actions.disable([self._entry()])
        self.assertTrue(results[0].ok, results[0].message)
        self.assertTrue(os.path.isfile(backup), "备份文件没写出来")

        raw = self._read(APPROVED_KEY, TEST_NAME)
        self.assertIsInstance(raw, bytes)
        self.assertEqual(len(raw), 12, f"标记应该是 12 字节，实际 {raw!r}")
        self.assertEqual(raw[0], 0x03, f"禁用标记首字节应该是 03，实际 {raw[0]:#x}")

        # 原值必须还在 —— 我们只动开关，不删用户的数据
        self.assertEqual(self._read(RUN_KEY, TEST_NAME), r"C:\Windows\system32\notepad.exe /test")

    def test_disable_is_visible_to_the_scanner(self):
        actions.disable([self._entry()])
        entries = autostart.scan_registry()
        found = [e for e in entries if e.name == TEST_NAME]
        self.assertEqual(len(found), 1, "扫描没找到刚建的测试条目")
        self.assertFalse(found[0].enabled, "扫描器没把 03 标记认成已禁用")

    def test_enable_restores_flag(self):
        actions.disable([self._entry()])
        results, _backup = actions.enable([self._entry()])
        self.assertTrue(results[0].ok, results[0].message)
        raw = self._read(APPROVED_KEY, TEST_NAME)
        self.assertEqual(raw[0], 0x02)
        self.assertEqual(raw, b"\x02" + b"\x00" * 11)

    def test_restore_from_backup_puts_original_bytes_back(self):
        # 先写一个「非标准」的旧值，确认还原是逐字节还原而不是硬编码回 02
        import winreg

        odd = bytes.fromhex("06000000aabbccddeeff0011")
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, TEST_NAME, 0, winreg.REG_BINARY, odd)
        finally:
            key.Close()

        results, backup = actions.disable([self._entry()])
        self.assertTrue(results[0].ok)
        self.assertEqual(self._read(APPROVED_KEY, TEST_NAME)[0], 0x03)

        restored, _path = actions.restore(backup)
        self.assertTrue(restored[0].ok, restored[0].message)
        self.assertEqual(self._read(APPROVED_KEY, TEST_NAME), odd, "还原没有回到原来的字节")

    def test_restore_deletes_flag_that_did_not_exist_before(self):
        # 改动前没有 StartupApproved 记录 → 还原应该把这条记录删掉
        self.assertIsNone(self._read(APPROVED_KEY, TEST_NAME))
        _results, backup = actions.disable([self._entry()])
        self.assertIsNotNone(self._read(APPROVED_KEY, TEST_NAME))
        actions.restore(backup)
        self.assertIsNone(self._read(APPROVED_KEY, TEST_NAME), "还原后应该没有这条记录了")

    def test_dry_run_changes_nothing(self):
        results, backup = actions.disable([self._entry()], dry_run=True)
        self.assertTrue(results[0].ok)
        self.assertIn("演练", results[0].message)
        self.assertEqual(backup, "")
        self.assertIsNone(self._read(APPROVED_KEY, TEST_NAME))

    def test_move_mode_renames_value_and_is_immediately_visible(self):
        results, backup = actions.disable([self._entry()], mode=actions.MODE_MOVE)
        self.assertTrue(results[0].ok, results[0].message)
        self.assertIsNone(self._read(RUN_KEY, TEST_NAME), "原值应该已经不在 Run 下了")
        renamed = self._read(RUN_KEY, actions.MOVE_PREFIX + TEST_NAME)
        self.assertEqual(renamed, r"C:\Windows\system32\notepad.exe /test")

        # 这条是「当场可验证」的：扫描器看不到它了
        entries = autostart.scan_registry()
        self.assertNotIn(TEST_NAME, [e.name for e in entries])

        back, _b = actions.enable([self._entry()], mode=actions.MODE_MOVE)
        self.assertTrue(back[0].ok, back[0].message)
        self.assertEqual(self._read(RUN_KEY, TEST_NAME), r"C:\Windows\system32\notepad.exe /test")
        self.assertIsNone(self._read(RUN_KEY, actions.MOVE_PREFIX + TEST_NAME))

    def test_protected_entry_is_refused_even_with_force(self):
        entry = self._entry()
        entry.protected = True
        results, backup = actions.disable([entry], force=True)
        self.assertFalse(results[0].ok)
        self.assertIn("系统自带", results[0].message)
        self.assertEqual(backup, "")
        self.assertIsNone(self._read(APPROVED_KEY, TEST_NAME), "被拒绝时不该写任何东西")

    def test_runonce_is_refused(self):
        entry = self._entry()
        entry.kind = "runonce"
        results, _backup = actions.disable([entry])
        self.assertFalse(results[0].ok)
        self.assertIn("一次性", results[0].message)

    def test_backup_file_is_valid_json(self):
        import json

        _results, backup = actions.disable([self._entry()])
        with open(backup, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIn("ops", payload)
        self.assertEqual(payload["ops"][0]["kind"], "flag")
        self.assertEqual(payload["ops"][0]["value_name"], TEST_NAME)


@unittest.skipUnless(WINDOWS, "只有 Windows 才有这些接口")
class TestRealScan(unittest.TestCase):
    """对真实系统扫一遍，确认不炸、且结果自洽。"""

    def test_scan_returns_entries(self):
        entries, _problems = autostart.scan(("registry", "folders"))
        self.assertGreater(len(entries), 0, "这台机器不可能一条自启都没有")

    def test_every_entry_has_a_locator(self):
        entries, _problems = autostart.scan(("registry", "folders"))
        for entry in entries:
            self.assertTrue(entry.locator, f"{entry.name} 没有 locator，改的时候会找不到它")

    def test_processes_have_memory_for_all(self):
        ps = winapi.processes()
        self.assertGreater(len(ps), 10)
        with_memory = [p for p in ps if p.working_set]
        # 全量内存接口可用时，绝大多数进程都该有数字
        if winapi.COVERAGE["mode"] == "all":
            self.assertGreater(len(with_memory) / len(ps), 0.5, "内存覆盖率太低")

    def test_memory_status_is_sane(self):
        total, avail = winapi.memory_status()
        self.assertGreater(total, 0)
        self.assertGreater(avail, 0)
        self.assertLessEqual(avail, total)

    def test_boot_time_is_in_the_past(self):
        import time

        boot = winapi.boot_time()
        self.assertGreater(boot, 0)
        self.assertLess(boot, time.time() + 5)

    def test_version_info_of_notepad(self):
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "system32", "notepad.exe")
        if not os.path.isfile(path):
            self.skipTest("找不到 notepad.exe")
        info = winapi.version_info(path)
        self.assertIn("Microsoft", info.get("company", ""))

    def test_protected_service_recognises_windows_services(self):
        entries, _problems = autostart.scan_services()
        if not entries:
            self.skipTest("读不到服务列表")
        svchost_ones = [e for e in entries if "svchost.exe" in (e.command or "").lower()]
        self.assertTrue(svchost_ones, "应该有跑在 svchost 里的服务")
        self.assertTrue(all(e.protected for e in svchost_ones), "svchost 承载的服务必须算系统组件")


if __name__ == "__main__":
    unittest.main()
