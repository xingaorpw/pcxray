"""真的改注册表的端到端测试。只在 Windows 上跑。

这个文件是整个项目最重要的证据：它证明「关掉一条自启」这件事**真的做到了**，
而不是写了个看起来很对的函数。

做法：在 `HKCU\\...\\Run` 下建一条自己的测试值（指向 notepad.exe），关掉它，
读回 `StartupApproved` 的字节确认，再还原，最后清理干净。全程不碰用户已有的
任何一条自启项。
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from pcxray import actions, autostart, safety, winapi

WINDOWS = sys.platform == "win32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
# 别改成 do_not_use 那一版：这台机器上的启动项清理程序（联想电脑管家之类）
# 会把那个确切的名字当成垃圾反复删掉，测试就会莫名其妙地「值不见了」。
TEST_NAME = "pcxray_selftest_probe"


@unittest.skipUnless(WINDOWS, "只有 Windows 才有注册表 Run 键")
class TestRegistryRoundTrip(unittest.TestCase):
    def setUp(self):
        import winreg

        self.winreg = winreg
        # 测试不许往用户真实的 ~/.pcxray/backup 里塞文件。以前就是这么干的，
        # 跑一遍测试就在人家目录里留下十几份备份。
        self._backup_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._backup_dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._backup_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
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
        # 有些机器上装了启动项清理程序（联想电脑管家之类），它会把刚写进去的
        # Run 值当成垃圾立刻删掉。那不是 pcxray 的问题 —— 跳过，别报假失败。
        if self._read(RUN_KEY, TEST_NAME) is None:
            self.skipTest("这台机器上的启动项清理程序把测试项删掉了，跳过注册表往返测试")

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

    def test_move_mode_takes_the_value_out_of_run(self):
        """--mode move 必须让值真的离开 Run。

        老做法是同一个键里改名成 `~pcxray~原名`。但 `Run` 里的**每一个**值
        Windows 都会执行（MS 文档：Run/RunOnce 的值都按命令行跑，只有 RunOnce
        的 `!`、`*` 前缀有别的含义），改名字拦不住启动 —— 那个「立刻生效」是假的。
        现在挪到 Windows 不读的 `Run-pcxray-disabled` 里。
        """
        disabled_key = RUN_KEY + autostart.DISABLED_SUFFIX
        results, backup = actions.disable([self._entry()], mode=actions.MODE_MOVE)
        self.assertTrue(results[0].ok, results[0].message)
        self.assertIsNone(self._read(RUN_KEY, TEST_NAME), "值必须离开 Run，否则照样会启动")
        self.assertEqual(self._read(disabled_key, TEST_NAME), r"C:\Windows\system32\notepad.exe /test")

        # 扫描器还看得见它，但标成禁用 —— 不然 `pcxray on` 就找不到东西可放回去
        listed = [e for e in autostart.scan_registry() if e.name == TEST_NAME]
        self.assertEqual(len(listed), 1, "挪走的条目应该还能被列出来（标成禁用）")
        self.assertFalse(listed[0].enabled)

        back, _b = actions.enable(listed, mode=actions.MODE_MOVE)
        self.assertTrue(back[0].ok, back[0].message)
        self.assertEqual(self._read(RUN_KEY, TEST_NAME), r"C:\Windows\system32\notepad.exe /test")
        self.assertIsNone(self._read(disabled_key, TEST_NAME))

    def test_move_mode_then_restore_puts_it_back(self):
        disabled_key = RUN_KEY + autostart.DISABLED_SUFFIX
        _r, backup = actions.disable([self._entry()], mode=actions.MODE_MOVE)
        self.assertIsNone(self._read(RUN_KEY, TEST_NAME))
        results, _p = actions.restore(backup)
        self.assertTrue(results[0].ok, results[0].message)
        self.assertEqual(self._read(RUN_KEY, TEST_NAME), r"C:\Windows\system32\notepad.exe /test")
        self.assertIsNone(self._read(disabled_key, TEST_NAME))

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

    def test_string_typed_approved_value_is_restored_not_deleted(self):
        """`StartupApproved` 里可能混着 REG_SZ 的值（本机真有一条）。

        第一版只记了 bytes 的 hex，字符串老值在还原时会被「删掉」—— 看起来
        成功了，其实丢了原来的数据。
        """
        import winreg

        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, TEST_NAME, 0, winreg.REG_SZ, "原来是字符串")
        finally:
            key.Close()

        _results, backup = actions.disable([self._entry()])
        self.assertEqual(self._read(APPROVED_KEY, TEST_NAME)[0], 0x03)

        restored, _path = actions.restore(backup)
        self.assertTrue(restored[0].ok, restored[0].message)
        self.assertEqual(
            self._read(APPROVED_KEY, TEST_NAME), "原来是字符串", "字符串老值没有还原回来"
        )

    def test_same_entry_twice_writes_one_backup_op(self):
        import json

        entry = self._entry()
        _results, backup = actions.disable([entry, entry])
        with open(backup, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(len(payload["ops"]), 1)


@unittest.skipUnless(WINDOWS, "只有 Windows 才有计划任务")
class TestScheduledTaskRoundTrip(unittest.TestCase):
    """真的建一条计划任务、真的关掉、用 schtasks 从外部确认状态。

    这一块以前完全没有验证过 —— 结果 `off <计划任务>` 每次都在**动作成功之后
    报失败**（脚本输出 `'ok'`，而 `run_json` 要求 JSON）。用户会以为没生效，
    然后反复重试。真机跑一遍才暴露出来。
    """

    NAME = "pcxray_selftest_task"

    def setUp(self):
        self._backup_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._backup_dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._backup_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        if not self._schtasks_available():
            self.skipTest("没有 schtasks")
        self._delete()

    def tearDown(self):
        self._delete()

    def _schtasks_available(self) -> bool:
        try:
            done = subprocess.run(
                ["schtasks", "/Query", "/TN", "nonexistent_task_pcxray"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return False
        return done.returncode in (0, 1)

    def _delete(self) -> None:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", self.NAME, "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _create(self, name: str = "") -> str:
        name = name or self.NAME
        when = (datetime.datetime.now() + datetime.timedelta(days=2)).strftime("%H:%M")
        done = subprocess.run(
            ["schtasks", "/Create", "/TN", name, "/TR", "notepad.exe /selftest",
             "/SC", "ONCE", "/ST", when, "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if done.returncode != 0:
            self.skipTest("建不了计划任务（可能需要管理员）：" + done.stderr.decode("mbcs", "replace")[:120])
        return name

    def _state(self, name: str = "") -> str:
        # 不能用 `schtasks /FO LIST` 解析 —— 中文系统上那一行是「状态:」，
        # 按 "status" 匹配会拿到空串（第一版就是这么挂的）。
        # `Get-ScheduledTask` 的 State 是枚举名（Ready/Disabled），不受语言影响。
        from pcxray import pwsh

        ok, out, _reason = pwsh.run_text(
            f"Get-ScheduledTask -TaskName {pwsh.ps_quote(name or self.NAME)} -ErrorAction Stop"
            f" | Select-Object -ExpandProperty State",
            timeout=30,
        )
        return out.splitlines()[-1].strip() if ok and out else ""

    def _entry(self, name: str = ""):
        name = name or self.NAME
        return autostart.Entry(
            name=name,
            kind="task",
            source="计划任务",
            scope="user",
            command="notepad.exe /selftest",
            exe="notepad.exe",
            enabled=True,
            locator={"path": "\\", "name": name},
        )

    def test_disable_then_enable_is_reported_truthfully(self):
        self._create()
        self.assertEqual(self._state(), "Ready")

        results, _backup = actions.disable([self._entry()])
        self.assertTrue(results[0].ok, results[0].message)
        self.assertEqual(self._state(), "Disabled", "报告成功但任务其实没被禁用")

        results, _backup = actions.enable([self._entry()])
        self.assertTrue(results[0].ok, results[0].message)
        self.assertEqual(self._state(), "Ready", "报告成功但任务其实没被启用")

    def test_restore_re_enables_the_task(self):
        self._create()
        _results, backup = actions.disable([self._entry()])
        self.assertEqual(self._state(), "Disabled")

        restored, _path = actions.restore(backup)
        self.assertTrue(restored[0].ok, restored[0].message)
        self.assertEqual(self._state(), "Ready", "还原报成功但任务还是禁用状态")

    def test_apostrophe_in_task_name_does_not_break_the_script(self):
        # 不转义的话 PowerShell 直接语法错误，而报出来的是「输出不是合法 JSON」
        name = "pcxray_o'brien"
        self._create(name)
        try:
            results, _backup = actions.disable([self._entry(name)])
            self.assertTrue(results[0].ok, results[0].message)
            self.assertEqual(self._state(name), "Disabled")
        finally:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", name, "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def test_missing_task_reports_failure_not_success(self):
        results, _backup = actions.disable([self._entry("pcxray_no_such_task_xyz")])
        self.assertFalse(results[0].ok)
        self.assertNotIn("状态：Disabled", results[0].message)


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
        # 按 **exe 名字**筛，不按子串 —— `mysvchost.exe` 这种第三方程序名字里
        # 也含 "svchost.exe"，用子串筛会把它误算进来，然后断言失败
        svchost_ones = [e for e in entries if safety.exe_of(e.command) == "svchost.exe"]
        self.assertTrue(svchost_ones, "应该有跑在 svchost 里的服务")
        loose = [e for e in svchost_ones if not e.protected]
        # 失败时把出问题的那几条原样打出来 —— 只看到「False is not true」根本
        # 不知道是哪一条、命令行长什么样，那就得靠猜
        detail = "\n".join(
            f"  {e.name}: command={e.command!r} exe={e.exe!r} note={e.note!r}" for e in loose
        )
        self.assertFalse(loose, f"这些 svchost 服务没被认成系统组件：\n{detail}")


class TestMoveModeNeverOverwrites(unittest.TestCase):
    """挪值的时候目标已经有别的值，就必须停下。

    以前直接写：目标存在时 Windows 会覆盖，返回值还是「已改名为 …」。用户的一条
    自启项就这么没了，事后完全看不出来 —— 工具承诺过「不删你的东西」。
    """

    KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    OFF = KEY + autostart.DISABLED_SUFFIX

    def setUp(self):
        if not winapi.IS_WINDOWS:
            self.skipTest("注册表只有 Windows 有")
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(actions, "BACKUP_DIR", self._dir.name)
        self._patch.start()
        self.name = "pcxray_collide_test"
        self._write(self.KEY, self.name, '"C:\\Windows\\System32\\notepad.exe" /A')
        self._write(self.OFF, self.name, '"C:\\Windows\\System32\\notepad.exe" /B')

    def tearDown(self):
        for key in (self.KEY, self.OFF):
            self._delete(key, self.name)
        self._patch.stop()
        self._dir.cleanup()

    def _write(self, key, name, value):
        import winreg

        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        k.Close()

    def _delete(self, key, name):
        import winreg

        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(k, name)
            k.Close()
        except OSError:
            pass

    def _read(self, key, name):
        import winreg

        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key)
            value, _kind = winreg.QueryValueEx(k, name)
            k.Close()
            return value
        except OSError:
            return None

    def _entry(self):
        entries, _ = autostart.scan(("registry",))
        for e in entries:
            if e.name == self.name and not e.locator.get("moved"):
                return e
        self.fail("没扫到刚写进去的测试条目")

    def test_disable_refuses_and_keeps_both_values(self):
        results, _backup = actions.disable([self._entry()], mode="move")
        self.assertFalse(results[0].ok, results[0].message)
        self.assertIn("已经有一条同名的值", results[0].message)
        self.assertEqual(self._read(self.KEY, self.name), '"C:\\Windows\\System32\\notepad.exe" /A')
        self.assertEqual(self._read(self.OFF, self.name), '"C:\\Windows\\System32\\notepad.exe" /B')

    def test_identical_value_is_not_a_conflict(self):
        # 两边指向同一条命令时挪过去不会丢信息，不该拦
        self._write(self.OFF, self.name, '"C:\\Windows\\System32\\notepad.exe" /A')
        results, _backup = actions.disable([self._entry()], mode="move")
        self.assertTrue(results[0].ok, results[0].message)
        self.assertEqual(self._read(self.OFF, self.name), '"C:\\Windows\\System32\\notepad.exe" /A')
        self.assertIsNone(self._read(self.KEY, self.name))


if __name__ == "__main__":
    unittest.main()
