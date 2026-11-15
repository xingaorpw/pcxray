"""`actions._run` 的调度逻辑，不需要碰注册表。

这里验的是「什么该跳过」「备份失败就不能动手」这类承诺 —— 它们是这个工具
敢去改系统的前提。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from pcxray import actions, autostart, pwsh


def entry(name="X", kind="run", scope="user", protected=False):
    return autostart.Entry(
        name=name,
        kind=kind,
        source="测试",
        scope=scope,
        command="x.exe",
        exe="x.exe",
        protected=protected,
    )


class RunCase(unittest.TestCase):
    def setUp(self):
        self.applied = []
        self.backups = []

        def fake_plan(item, turn_on, mode):
            return {"kind": "flag", "name": item.name, "subkey": "Run", "value_name": item.name}

        def fake_apply(plan, turn_on):
            self.applied.append(plan["name"])
            return actions.Result(plan["name"], True, "改了")

        def fake_backup(ops):
            self.backups.append(ops)
            return "/tmp/backup.json"

        for name, fake in (
            ("_plan", fake_plan),
            ("_apply", fake_apply),
            ("_save_backup", fake_backup),
            ("_is_admin", lambda: True),
        ):
            patcher = mock.patch.object(actions, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestSkips(RunCase):
    def test_protected_is_skipped_even_with_force(self):
        results, backup = actions.disable([entry(protected=True)], force=True)
        self.assertFalse(results[0].ok)
        self.assertIn("系统自带", results[0].message)
        self.assertEqual(self.applied, [])
        self.assertEqual(backup, "")

    def test_runonce_is_skipped(self):
        results, _ = actions.disable([entry(kind="runonce")])
        self.assertFalse(results[0].ok)
        self.assertIn("一次性", results[0].message)
        self.assertEqual(self.applied, [])

    def test_service_needs_force(self):
        results, _ = actions.disable([entry(kind="service", scope="machine")])
        self.assertFalse(results[0].ok)
        self.assertIn("--force", results[0].message)
        self.assertEqual(self.applied, [])

    def test_service_allowed_with_force(self):
        results, backup = actions.disable([entry(kind="service", scope="machine")], force=True)
        self.assertTrue(results[0].ok)
        self.assertEqual(self.applied, ["X"])
        self.assertEqual(backup, "/tmp/backup.json")

    def test_non_admin_cannot_touch_machine_scope(self):
        with mock.patch.object(actions, "_is_admin", lambda: False):
            results, _ = actions.disable([entry(scope="machine")])
        self.assertFalse(results[0].ok)
        self.assertIn("管理员", results[0].message)
        self.assertEqual(self.applied, [])


class TestDedupe(RunCase):
    def test_same_entry_twice_is_applied_once(self):
        item = entry()
        results, backup = actions.disable([item, item])
        self.assertEqual(self.applied, ["X"])
        self.assertEqual(len(self.backups[0]), 1)
        self.assertEqual(len(results), 1)

    def test_two_different_entries_both_apply(self):
        results, _ = actions.disable([entry("A"), entry("B")])
        self.assertEqual(sorted(self.applied), ["A", "B"])


class TestBackupGate(RunCase):
    def test_backup_failure_aborts_before_touching_anything(self):
        # 没有退路的事情不做：备份写不进去就一个字节都不改
        with mock.patch.object(actions, "_save_backup", side_effect=OSError("磁盘满了")):
            results, backup = actions.disable([entry()])
        self.assertEqual(self.applied, [], "备份失败时竟然还是改了系统")
        self.assertEqual(backup, "")
        self.assertFalse(any(r.ok for r in results))
        self.assertTrue(any("备份" in r.message for r in results))

    def test_dry_run_changes_nothing(self):
        results, backup = actions.disable([entry()], dry_run=True)
        self.assertEqual(self.applied, [])
        self.assertEqual(backup, "")
        self.assertTrue(results[0].ok)
        self.assertIn("演练", results[0].message)


class TestBackupFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_and_list(self):
        path = actions._save_backup([{"kind": "flag", "name": "X"}])
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["ops"][0]["name"], "X")
        self.assertIn("created", payload)
        self.assertIn(path, actions.list_backups())

    def test_list_ignores_non_json(self):
        with open(os.path.join(self.tmp.name, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        self.assertEqual(actions.list_backups(), [])

    def test_two_backups_in_the_same_second_do_not_collide(self):
        first = actions._save_backup([{"kind": "flag", "name": "A"}])
        second = actions._save_backup([{"kind": "flag", "name": "B"}])
        self.assertNotEqual(first, second)
        self.assertEqual(len(actions.list_backups()), 2)

    def test_no_tmp_file_left_behind(self):
        actions._save_backup([{"kind": "flag"}])
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_list_is_newest_first(self):
        made = [actions._save_backup([{"kind": "flag", "name": name}]) for name in ("A", "B", "C")]
        # 断言的是「新的在前」这个意思，不是「按字符串倒序」—— 后者只在
        # 同一秒没有重名后缀时才和前者等价（上一版就是这么写的，于是没发现
        # 同秒多份时顺序是反的）。
        self.assertEqual(actions.list_backups(), list(reversed(made)))

    def test_same_second_backups_sort_newest_first(self):
        """同一秒里连写 12 份，最新的必须排第一。

        名字长这样：`20260101-000000.json`、`-1.json` … `-11.json`。
        按字符串排是错的：`-` 比 `.` 小，所以不带后缀的那份（最老）排最前，
        而 `-10`、`-11` 排到 `-2` 前面。实测 12 份时 `list_backups()` 的
        第一名是最老那份 —— `restore` 不带参数就会还原到最旧的状态。
        """
        stamp = "20260101-000000"
        names = [f"{stamp}.json"] + [f"{stamp}-{i}.json" for i in range(1, 13)]
        for name in names:
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as handle:
                handle.write("{}")
        listed = [os.path.basename(p) for p in actions.list_backups()]
        self.assertEqual(listed[0], f"{stamp}-12.json", f"第一名不是最新的：{listed[:3]}")
        self.assertEqual(listed[-1], f"{stamp}.json", f"最后一名不是最老的：{listed[-3:]}")
        self.assertEqual(listed, list(reversed(names)))

    def test_prune_keeps_the_newest_of_a_same_second_batch(self):
        stamp = "20260101-000000"
        names = [f"{stamp}.json"] + [f"{stamp}-{i}.json" for i in range(1, 6)]
        for name in names:
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as handle:
                handle.write("{}")
        actions.prune(2)
        left = sorted(os.listdir(self.tmp.name))
        self.assertEqual(left, [f"{stamp}-4.json", f"{stamp}-5.json"], f"留下的不是最新的两份：{left}")

    def test_unknown_backup_name_does_not_break_sorting(self):
        with open(os.path.join(self.tmp.name, "20260101-000000.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(os.path.join(self.tmp.name, "我自己改的名字.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        listed = [os.path.basename(p) for p in actions.list_backups()]
        self.assertEqual(len(listed), 2, "认不出名字的备份不能丢")
        self.assertEqual(listed[0], "20260101-000000.json", f"认不出的名字应该排后面：{listed}")

    def test_parallel_saves_all_get_unique_files(self):
        """同一秒里 12 个请求同时要备份，不能有两个抢到同一个文件。

        网页是多线程的。以前「先 exists 判重、再写 .tmp」两步之间没有原子性：
        并发时第二个请求报 `WinError 32 另一个程序正在使用此文件`，用户看到
        「备份写不进去，已中止」—— 明明什么都没改，却被吓一跳。
        """
        import threading

        paths: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(i):
            try:
                got = actions._save_backup([{"kind": "flag", "name": f"x{i}"}])
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
                return
            with lock:
                paths.append(got)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"并发写备份报错了：{errors}")
        self.assertEqual(len(paths), 12)
        self.assertEqual(len(set(paths)), 12, "有两个请求拿到了同一个备份路径")
        # 每个都必须是完整可读的 JSON，不能是半截
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                self.assertIn("ops", json.load(handle))


class TestRestoreEdge(unittest.TestCase):
    def test_missing_backup_reports_clearly(self):
        results, _ = actions.restore("/definitely/not/here.json")
        self.assertFalse(results[0].ok)
        self.assertIn("没有这个备份文件", results[0].message)
        self.assertIn("/definitely/not/here.json", results[0].message)

    def test_no_backups_at_all(self):
        with mock.patch.object(actions, "list_backups", lambda: []):
            results, _ = actions.restore("")
        self.assertFalse(results[0].ok)
        self.assertIn("没有任何备份", results[0].message)

    def test_broken_json_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            results, _ = actions.restore(path)
            self.assertFalse(results[0].ok)
            self.assertIn("不是合法 JSON", results[0].message)

    def test_unknown_op_kind_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "odd.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"ops": [{"kind": "teleport", "name": "X"}]}, handle)
            results, _ = actions.restore(path)
            self.assertFalse(results[0].ok)
            self.assertIn("不认识", results[0].message)

    def test_dry_run_does_not_touch_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"ops": [{"kind": "flag", "name": "X", "hive": "HKCU",
                                    "subkey": "X", "value_name": "X"}]}, handle)
            with mock.patch.object(actions, "_write_value") as write, mock.patch.object(
                actions, "_delete_value"
            ) as delete:
                results, _ = actions.restore(path, dry_run=True)
            write.assert_not_called()
            delete.assert_not_called()
            self.assertTrue(results[0].ok)


class TestDescribe(unittest.TestCase):
    def test_flag(self):
        text = actions._describe({"kind": "flag", "subkey": "A\\B\\Run"}, False)
        self.assertIn("Run", text)

    def test_move(self):
        text = actions._describe({"kind": "move", "new_name": "~pcxray~x"}, False)
        self.assertIn("~pcxray~x", text)

    def test_task(self):
        self.assertIn("计划任务", actions._describe({"kind": "task"}, True))

    def test_service(self):
        self.assertIn("自动", actions._describe({"kind": "service"}, True))


class TestRestoreSkipsUnusableBackups(unittest.TestCase):
    """`restore` 不带参数时跳过读不出来的备份。

    备份写到一半断电、磁盘满、文件被人手改过 —— 最新那份就废了。只认「读得
    出来、结构对、里面有 ops」的，不然用户敲 `pcxray restore` 只会得到一句
    「结构不对」，明明前面还有好几份好的。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, name, payload):
        path = os.path.join(self._dir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle)
        return path

    def _good_op(self):
        return {
            "kind": "flag",
            "hive": "HKCU",
            "subkey": r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
            "value_name": "pcxray_no_such_value",
            "name": "pcxray_no_such_value",
            "existed": False,
            "old": None,
            "old_str": None,
            "old_kind": None,
            "new": "03",
        }

    def test_picks_older_good_one_and_says_it_skipped(self):
        self._write("20260101-000001.json", {"created": "2026-01-01 00:00:01", "ops": [self._good_op()]})
        self._write("20260101-000002.json", "{半截")  # 更新的那份坏了
        results, path = actions.restore(dry_run=True)
        self.assertTrue(path.endswith("20260101-000001.json"), path)
        self.assertTrue(any("跳过" in r.message for r in results), [r.message for r in results])
        self.assertTrue(any("20260101-000002.json" in r.message for r in results))

    def test_all_unusable_is_a_clean_failure(self):
        self._write("20260101-000001.json", "{半截")
        self._write("20260101-000002.json", {"ops": []})
        results, path = actions.restore(dry_run=True)
        self.assertEqual(path, "")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("都读不出来", results[0].message)

    def test_usable_check_rejects_the_shapes_that_would_crash(self):
        self.assertFalse(actions._backup_is_usable(self._write("a.json", {"ops": "x"})))
        self.assertFalse(actions._backup_is_usable(self._write("b.json", {})))
        self.assertFalse(actions._backup_is_usable(self._write("c.json", {"ops": [1, 2]})))
        self.assertTrue(actions._backup_is_usable(self._write("d.json", {"ops": [self._good_op()]})))

    def test_usable_check_on_missing_file(self):
        self.assertFalse(actions._backup_is_usable(os.path.join(self._dir.name, "nope.json")))


class TestFolderItemInMoveMode(unittest.TestCase):
    """启动文件夹的项没有「改名的值」这回事。

    以前只丢一句「不知道怎么改这一条」—— 用户不知道是自己用错了模式还是工具不支持。
    """

    def _entry(self):
        return autostart.Entry(
            name="某快捷方式.lnk",
            kind="folder",
            source="启动文件夹 · 当前用户",
            scope="user",
            command=r"C:\x\a.exe",
            exe=r"C:\x\a.exe",
            locator={
                "hive": "HKCU",
                "subkey": "StartupFolder",
                "value": "某快捷方式.lnk",
                "path": r"C:\x\a.lnk",
            },
        )

    def test_move_mode_says_which_mode_to_use(self):
        results, backup = actions.disable([self._entry()], mode=actions.MODE_MOVE, dry_run=True)
        self.assertEqual(backup, "")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("没法用 --mode move", results[0].message)
        self.assertIn("--mode move", results[0].message)

    def test_flag_mode_still_plans_a_change(self):
        plan = actions._plan(self._entry(), False, actions.MODE_FLAG)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "flag")
        self.assertIn("StartupApproved", plan["subkey"])


class TestFolderItemIsFoundByFileNotRegistry(unittest.TestCase):
    r"""启动文件夹的项在不在，要看那个文件在不在。

    备份记录里文件夹项的 `target_subkey` 是 `StartupFolder` —— 那是
    `StartupApproved` 的栏名，不是注册表路径。以前一律拿它去 `HKCU\StartupFolder`
    查值，查不到就报「这一条已经不在了，跳过」，于是**每一次** restore 一条
    启动文件夹的项都什么也没做，开关标记永远删不掉。实测过。
    """

    def _op(self, name: str) -> dict:
        return {
            "kind": "flag",
            "hive": "HKCU",
            "subkey": r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder",
            "value_name": name,
            "target_subkey": "StartupFolder",
            "old": "020000000000000000000000",
            "old_str": None,
        }

    def test_shortcut_still_there_counts_as_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "有.lnk"), "w", encoding="utf-8") as handle:
                handle.write("x")
            with mock.patch.object(
                actions.autostart, "_startup_dirs", return_value=[("user", tmp, "启动文件夹 · 当前用户")]
            ):
                self.assertTrue(actions._item_still_there(self._op("有.lnk")))

    def test_shortcut_gone_counts_as_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                actions.autostart, "_startup_dirs", return_value=[("user", tmp, "启动文件夹 · 当前用户")]
            ):
                self.assertFalse(actions._item_still_there(self._op("没有.lnk")))

    def test_registry_items_still_use_the_value_check(self):
        op = {
            "kind": "flag",
            "hive": "HKCU",
            "subkey": r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
            "value_name": "X",
            "target_subkey": r"Software\Microsoft\Windows\CurrentVersion\Run",
            "old": None,
            "old_str": None,
        }
        with mock.patch.object(actions, "_value_exists", return_value=True) as spy:
            self.assertTrue(actions._item_still_there(op))
        spy.assert_called_once_with("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", "X")


class TestRestoreGivenADirectory(unittest.TestCase):
    """传目录进来时说清楚是目录，不要报成「Permission denied」。

    Windows 上 open() 一个目录抛的是 Permission denied —— 用户看到这句会去查
    权限、改 ACL，其实只是路径写成了文件夹。
    """

    def test_directory_is_named_as_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            done, path = actions.restore(tmp)
        self.assertFalse(done[0].ok)
        self.assertIn("那是个目录", done[0].message)
        self.assertNotIn("Permission denied", done[0].message)
        self.assertNotIn("读备份失败", done[0].message)


class TestRestoreMalformedRecords(unittest.TestCase):
    """备份可能是手改过的、写到一半断电的。

    以前 `bytes.fromhex("zzz")` 直接把 ValueError 抛出去（一屏 traceback），
    缺 `was_enabled` 时会替备份编一个「原本就是禁用状态」—— 它根本没记这件事。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore(self, op):
        path = os.path.join(self._dir.name, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "ops": [op]}, handle)
        with mock.patch.object(actions, "_subkey_exists", return_value=True), mock.patch.object(
            actions, "_value_exists", return_value=True
        ), mock.patch.object(actions, "_write_value"), mock.patch.object(actions, "_read_value"):
            return actions.restore(path)[0]

    def test_bad_hex_is_a_clean_failure(self):
        results = self._restore(
            {
                "kind": "flag",
                "name": "X",
                "hive": "HKCU",
                "subkey": r"Software\Microsoft\Windows\CurrentVersion\Run",
                "value_name": "X",
                "existed": True,
                "old": "zzz",
            }
        )
        self.assertFalse(results[0].ok)
        self.assertIn("不是合法的十六进制", results[0].message)

    def test_missing_subkey_says_which_field(self):
        results = self._restore({"kind": "flag", "name": "X"})
        self.assertFalse(results[0].ok)
        self.assertIn("缺 subkey", results[0].message)

    def test_task_without_was_enabled_does_not_claim_it_was_disabled(self):
        results = self._restore({"kind": "task", "name": "X", "task": "X"})
        self.assertFalse(results[0].ok)
        self.assertIn("缺 was_enabled", results[0].message)
        self.assertNotIn("原本就是禁用", results[0].message)

    def test_service_without_was_enabled_does_not_claim_it_was_manual(self):
        results = self._restore({"kind": "service", "name": "X", "service": "X"})
        self.assertFalse(results[0].ok)
        self.assertIn("缺 was_enabled", results[0].message)


class TestRestoreOldStyleMoveBackup(unittest.TestCase):
    """老备份里的 move 记的是「同一个键里改名」（没有 target_subkey）。

    本机上真的存在 52 份这样的备份 —— 旧版 --mode move 写出来的。还原要能处理。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_old_backup_renames_back_in_the_same_key(self):
        path = os.path.join(self._dir.name, "old.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": 1,
                    "ops": [
                        {
                            "kind": "move",
                            "name": "旧条目",
                            "hive": "HKCU",
                            "subkey": r"Software\Microsoft\Windows\CurrentVersion\Run",
                            "old_name": "旧条目",
                            "new_name": "~pcxray~旧条目",
                            "value": r"C:\x\a.exe",
                            "value_kind": 1,
                            "existed": True,
                        }
                    ],
                },
                handle,
            )
        written = {}
        with mock.patch.object(
            actions, "_subkey_exists", return_value=True
        ), mock.patch.object(
            actions, "_delete_value"
        ) as delete, mock.patch.object(
            actions, "_write_value", side_effect=lambda *a: written.setdefault(a[1:3], a[3])
        ), mock.patch.object(actions, "_read_value", side_effect=lambda h, k, v: (True, written.get((k, v), r"C:\x\a.exe"), 1)):
            results, _ = actions.restore(path)
        self.assertTrue(results[0].ok, results[0].message)
        delete.assert_called_once()
        self.assertEqual(delete.call_args[0][2], "~pcxray~旧条目", "要把改名后的那条删掉")
        self.assertIn((r"Software\Microsoft\Windows\CurrentVersion\Run", "旧条目"), written)


class TestSetServiceVerifiesByReadingBack(unittest.TestCase):
    """改完必须读回来对一遍，不能只凭 PowerShell 没报错就报成功。

    以前这两条只被真机往返测试覆盖（要管理员、要 PowerShell）；判断「算不算成功」
    的那段逻辑本身没有测试。
    """

    def test_service_manual_success(self):
        with mock.patch.object(pwsh, "run_text", return_value=(True, "Manual\n", "")):
            ok, message = actions._set_service("X", False)
        self.assertTrue(ok, message)
        self.assertIn("手动", message)

    def test_service_reports_when_readback_disagrees(self):
        with mock.patch.object(pwsh, "run_text", return_value=(True, "Auto\n", "")):
            ok, message = actions._set_service("X", False)
        self.assertFalse(ok)
        self.assertIn("改完读回来的启动方式是「Auto」", message)

    def test_service_propagates_powershell_reason(self):
        with mock.patch.object(
            pwsh, "run_text", return_value=(False, "", "拒绝访问")
        ):
            ok, message = actions._set_service("X", True)
        self.assertFalse(ok)
        self.assertIn("拒绝访问", message)

    def test_task_disabled_success(self):
        with mock.patch.object(pwsh, "run_text", return_value=(True, "Disabled\n", "")):
            ok, message = actions._set_task("\\", "T", False)
        self.assertTrue(ok, message)
        self.assertIn("Disabled", message)

    def test_task_reports_when_readback_disagrees(self):
        with mock.patch.object(pwsh, "run_text", return_value=(True, "Ready\n", "")):
            ok, message = actions._set_task("\\", "T", False)
        self.assertFalse(ok)
        self.assertIn("改完读回来的状态是「Ready」", message)

    def test_task_empty_readback_is_a_failure(self):
        # 读回来是空的：不知道成没成，不能当成功
        with mock.patch.object(pwsh, "run_text", return_value=(True, "", "")):
            ok, message = actions._set_task("\\", "T", True)
        self.assertFalse(ok)
        self.assertIn("空", message)


class TestServiceOnIsAlwaysANoop(unittest.TestCase):
    """列表里的服务都是 `StartMode='Auto'` 扫出来的，本来就开着。

    再调一次 Set-Service 等于什么都没改，却会存一份备份、报一句「已改成自动启动」。
    """

    def _entry(self):
        return autostart.Entry(
            name="SomeAutoSvc",
            kind="service",
            source="自动启动的服务",
            scope="machine",
            enabled=True,
            locator={"name": "SomeAutoSvc"},
        )

    def test_on_is_a_noop(self):
        plan = actions._plan(self._entry(), True, actions.MODE_FLAG)
        self.assertTrue(plan.get("noop"))
        self.assertIn("本来就是自动启动", plan["noop_message"])

    def test_off_still_plans_a_change(self):
        plan = actions._plan(self._entry(), False, actions.MODE_FLAG)
        self.assertEqual(plan["kind"], "service")
        self.assertEqual(plan["service"], "SomeAutoSvc")


class TestMovedEntryInFlagMode(unittest.TestCase):
    """被 --mode move 挪走的条目，标记模式对它没有意义。

    写一个「启用」标记不会把值放回 Run，程序照样不启动 —— 报成功就是在骗人。
    """

    def _entry(self):
        return autostart.Entry(
            name="某条目",
            kind="run",
            source="注册表 · 当前用户 Run",
            scope="user",
            enabled=False,
            command=r"C:\x\a.exe",
            exe=r"C:\x\a.exe",
            locator={
                "hive": "HKCU",
                "subkey": r"Software\Microsoft\Windows\CurrentVersion\Run-pcxray-disabled",
                "value": "某条目",
                "moved": True,
            },
        )

    def test_enable_refuses_and_points_at_move_mode(self):
        plan = actions._plan(self._entry(), True, actions.MODE_FLAG)
        self.assertIn("refuse", plan)
        self.assertIn("--mode move", plan["refuse"])

    def test_enable_returns_failure_without_writing(self):
        results, backup = actions.enable([self._entry()])
        self.assertEqual(backup, "")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("--mode move", results[0].message)

    def test_disable_is_a_noop_with_its_own_wording(self):
        results, backup = actions.disable([self._entry()])
        self.assertEqual(backup, "")
        self.assertTrue(results[0].ok)
        self.assertIn("不用再关", results[0].message)

    def test_move_mode_still_puts_it_back(self):
        # 同一个条目用 --mode move 是有得办的（这里只验计划，不碰注册表）
        with mock.patch.object(actions, "_read_value", return_value=(True, r"C:\x\a.exe", 1)):
            plan = actions._plan(self._entry(), True, actions.MODE_MOVE)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "move")
        self.assertTrue(plan["subkey"].endswith(autostart.DISABLED_SUFFIX))
        self.assertFalse(plan["target_subkey"].endswith(autostart.DISABLED_SUFFIX))


class TestRestoreRefusesWhatItCannotRead(unittest.TestCase):
    """认不出来的东西不能报「已还原」。

    备份可能是新版本 pcxray 写的、被手改过、从别的机器拷来的。以前不认识
    `kind` 就直接跳过，一条都没还成也照样退出 0 —— 用户以为注册表回去了。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, payload) -> str:
        path = os.path.join(self._dir.name, "20260101-000000.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_unknown_kind_is_a_clean_failure(self):
        path = self._write({"version": 1, "ops": [{"kind": "future_thing", "name": "Z"}]})
        results, _ = actions.restore(path, dry_run=True)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("没有这个版本能还原的内容", results[0].message)

    def test_missing_kind_does_not_crash(self):
        # 以前会 KeyError：`op["kind"] in (...)` 直接抛
        path = self._write({"version": 1, "ops": [{"name": "Z"}]})
        results, _ = actions.restore(path, dry_run=True)
        self.assertFalse(results[0].ok)
        self.assertIn("没有这个版本能还原的内容", results[0].message)

    def test_mixed_backup_reports_the_unknown_one(self):
        path = self._write(
            {
                "version": 1,
                "ops": [
                    {"kind": "future_thing", "name": "Z"},
                    {"kind": "task", "name": "T", "task": "T", "path": "\\", "was_enabled": False},
                ],
            }
        )
        results, _ = actions.restore(path, dry_run=False)
        bad = [r for r in results if not r.ok]
        self.assertEqual(len(bad), 1)
        self.assertIn("future_thing", bad[0].message)
        # 认识的那条还是要办
        self.assertTrue(any("原本就是禁用状态" in r.message for r in results))

    def test_future_version_warns_but_still_restores(self):
        path = self._write(
            {
                "version": 99,
                "ops": [{"kind": "task", "name": "T", "task": "T", "path": "\\", "was_enabled": False}],
            }
        )
        results, _ = actions.restore(path, dry_run=True)
        self.assertTrue(any("v99" in r.message and "只认 v1" in r.message for r in results))
        self.assertTrue(any("将还原" in r.message for r in results))


class TestPruneKeepsTheNewest(unittest.TestCase):
    """备份会无限长。只留最近的若干份，而且只碰自己的目录。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make(self, count: int) -> list[str]:
        paths = []
        for i in range(count):
            path = os.path.join(self._dir.name, f"2026010{i + 1}-000000.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"created": "2026-01-01 00:00:00", "ops": []}, handle)
            paths.append(path)
        return paths

    def test_keeps_the_newest_and_deletes_the_rest(self):
        paths = self._make(5)
        removed, kept, freed = actions.prune(2)
        self.assertEqual(len(removed), 3)
        self.assertEqual(kept, 2)
        self.assertGreater(freed, 0)
        left = sorted(os.path.basename(p) for p in actions.list_backups())
        self.assertEqual(left, [os.path.basename(paths[3]), os.path.basename(paths[4])])
        # 删掉的必须是最旧的那三份
        self.assertEqual(sorted(os.path.basename(p) for p in removed),
                         sorted(os.path.basename(p) for p in paths[:3]))

    def test_nothing_to_do(self):
        self._make(3)
        removed, kept, freed = actions.prune(10)
        self.assertEqual((removed, kept, freed), ([], 3, 0))

    def test_zero_keep_is_refused(self):
        self._make(3)
        with self.assertRaises(ValueError):
            actions.prune(0)
        self.assertEqual(len(actions.list_backups()), 3, "拒绝了就不能真删")

    def test_dry_run_deletes_nothing(self):
        self._make(4)
        removed, kept, _freed = actions.prune(1, dry_run=True)
        self.assertEqual(len(removed), 3)
        self.assertEqual(kept, 1)
        self.assertEqual(len(actions.list_backups()), 4, "演练不能真删")

    def test_other_files_are_left_alone(self):
        self._make(2)
        stray = os.path.join(self._dir.name, "notes.txt")
        with open(stray, "w", encoding="utf-8") as handle:
            handle.write("别删我")
        removed, _kept, _freed = actions.prune(1)
        self.assertEqual(len(removed), 1)
        self.assertTrue(os.path.isfile(stray), "只该删 .json")

    def test_empty_dir_is_fine(self):
        self.assertEqual(actions.prune(5), ([], 0, 0))


class TestRepeatedChangesAreNoOps(unittest.TestCase):
    """已经是目标状态就别再写一遍。

    重复 `off` 一条本来就关着的项，以前会又写一份备份、又报「已写入开关标记」——
    用户会以为刚才那次没生效，备份目录也跟着长胖。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.written: list[tuple] = []
        self.state: bytes | None = None

    def _entry(self):
        return autostart.Entry(
            name="Probe",
            kind="run",
            source="用户 Run",
            scope="user",
            command=r"C:\Windows\System32\notepad.exe",
            exe=r"C:\Windows\System32\notepad.exe",
            enabled=True,
            locator={
                "hive": "HKCU",
                "subkey": r"Software\Microsoft\Windows\CurrentVersion\Run",
                "value": "Probe",
            },
        )

    def _fake_state(self, first_byte: bytes | None):
        """假装 StartupApproved 里现在是这个状态（None = 从来没有过）。"""
        self.state = None if first_byte is None else first_byte + b"\x00" * 11

        def read(hive, subkey, name):
            if "StartupApproved" in subkey:
                return (self.state is not None, self.state, 3)
            return (True, "x", 1)

        def write(hive, subkey, name, data):
            self.written.append((hive, subkey, name, data))
            if "StartupApproved" in subkey:
                self.state = data

        for patcher in (
            mock.patch.object(actions, "_read_value", side_effect=read),
            mock.patch.object(actions, "_write_value", side_effect=write),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_off_on_already_disabled_is_a_no_op(self):
        self._fake_state(b"\x03")
        results, backup = actions.disable([self._entry()])
        self.assertEqual(backup, "", "不该写备份")
        self.assertEqual(self.written, [], "不该动注册表")
        self.assertTrue(results[0].ok)
        self.assertIn("本来就是关着的", results[0].message)

    def test_on_when_never_disabled_is_a_no_op(self):
        self._fake_state(None)
        results, backup = actions.enable([self._entry()])
        self.assertEqual(backup, "")
        self.assertEqual(self.written, [])
        self.assertIn("本来就是开着的", results[0].message)

    def test_off_on_enabled_item_still_writes(self):
        self._fake_state(b"\x06")
        results, backup = actions.disable([self._entry()])
        self.assertTrue(backup, "改了就得备份")
        self.assertEqual(len(self.written), 1)
        # 提示要说清方向：off 和 on 以前都是「已写入开关标记」，看不出这条现在是开是关
        self.assertIn("已关掉", results[0].message)

    def test_on_after_another_tool_disabled_it_still_writes(self):
        # 0x04 是别的工具留下的「关」标记，对它执行 on 是真改动
        self._fake_state(b"\x04")
        results, backup = actions.enable([self._entry()])
        self.assertTrue(backup)
        self.assertEqual(len(self.written), 1)


class TestRestoreDoesNotRecreateStaleFlags(unittest.TestCase):
    """自启项被删掉之后，还原不该再往 StartupApproved 里写标记。

    标记本身看不见，但它对应的自启项已经不存在了 —— 往用户的注册表里留一条谁也
    不认识的记录，和「还原」这个词的意思不一样。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(actions, "BACKUP_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_flag_op_for_a_deleted_item_is_skipped(self):
        op = {
            "kind": "flag",
            "hive": "HKCU",
            "subkey": r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
            "target_subkey": r"Software\Microsoft\Windows\CurrentVersion\Run",
            "value_name": "pcxray_definitely_not_there",
            "name": "pcxray_definitely_not_there",
            "existed": True,
            "old": "02" + "00" * 10,
            "old_str": None,
            "old_kind": 3,
            "new": "03" + "00" * 10,
        }
        path = os.path.join(self._dir.name, "b.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"created": "2026-11-15 03:00:00", "ops": [op]}, handle)
        results, _used = actions.restore(path)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertIn("已经不在了", results[0].message)

    def test_string_valued_old_flag_is_restored_even_if_item_is_gone(self):
        # 老值是字符串 = 用户原来的数据，不能因为自启项没了就跳过
        op = {
            "kind": "flag",
            "hive": "HKCU",
            "subkey": r"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run",
            "target_subkey": r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
            "value_name": "pcxray_definitely_not_there",
            "name": "pcxray_definitely_not_there",
            "existed": True,
            "old": None,
            "old_str": "原来是字符串",
            "old_kind": 1,
            "new": "03" + "00" * 10,
        }
        path = os.path.join(self._dir.name, "str.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"created": "2026-11-15 03:00:00", "ops": [op]}, handle)
        results, _used = actions.restore(path, dry_run=True)
        self.assertEqual(len(results), 1)
        self.assertNotIn("已经不在了", results[0].message)

    def test_op_without_target_subkey_still_restores(self):
        # 老备份没有这个字段，不能因为「不知道」就跳过
        op = {
            "kind": "flag",
            "hive": "HKCU",
            "subkey": r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
            "value_name": "pcxray_old_backup_shape",
            "name": "pcxray_old_backup_shape",
            "existed": False,
            "old": None,
            "old_str": None,
            "old_kind": None,
            "new": "03",
        }
        path = os.path.join(self._dir.name, "old.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"created": "2026-11-15 03:00:00", "ops": [op]}, handle)
        results, _used = actions.restore(path, dry_run=True)
        self.assertEqual(len(results), 1)
        self.assertIn("将还原", results[0].message)


class TestDescribeBackup(unittest.TestCase):
    """列表里要能看出「哪份是刚才那次」。

    只有一串 `20261115-032457.json` 时用户没法挑，还原错一份比不还原更糟。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _write(self, payload):
        path = os.path.join(self._dir.name, "b.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_shows_time_and_count(self):
        path = self._write({"created": "2026-11-15T03:24:57", "ops": [{}, {}, {}]})
        when, count = actions.describe_backup(path)
        self.assertEqual(when, "2026-11-15 03:24:57")
        self.assertEqual(count, "3 项")

    def test_missing_created_says_unknown(self):
        when, count = actions.describe_backup(self._write({"ops": [{}]}))
        self.assertEqual(when, "时间未知")
        self.assertEqual(count, "1 项")

    def test_broken_file_says_so(self):
        path = os.path.join(self._dir.name, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        when, count = actions.describe_backup(path)
        self.assertEqual(when, "时间未知")
        self.assertIn("读不出来", count)

    def test_ops_not_a_list(self):
        _when, count = actions.describe_backup(self._write({"created": "2026-11-15T03:24:57", "ops": "x"}))
        self.assertIn("结构不对", count)


class TestRestoreRejectsBadFiles(unittest.TestCase):
    """备份坏了要报错，不能打 traceback，也不能一声不响地退出 0。

    还原是「反悔」路径，用户手里那份 JSON 可能是手改的、写到一半断电的、
    从别的机器拷来的。以前 `{"ops": "x"}` 抛 AttributeError，`{}` 返回空结果
    并让命令行退出 0 —— 用户以为还原成功了。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _write(self, text):
        path = os.path.join(self._dir.name, "b.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_not_json(self):
        results, _path = actions.restore(self._write("not json"))
        self.assertFalse(results[0].ok)
        self.assertIn("不是合法 JSON", results[0].message)

    def test_missing_file(self):
        results, _path = actions.restore(os.path.join(self._dir.name, "nope.json"))
        self.assertFalse(results[0].ok)
        self.assertIn("没有这个备份文件", results[0].message)

    def test_ops_is_not_a_list(self):
        results, _path = actions.restore(self._write('{"ops": "x"}'))
        self.assertFalse(results[0].ok)
        self.assertIn("结构不对", results[0].message)

    def test_ops_contains_non_dict(self):
        results, _path = actions.restore(self._write('{"ops": [1, 2]}'))
        self.assertFalse(results[0].ok)
        self.assertIn("结构不对", results[0].message)

    def test_empty_backup_is_not_silent_success(self):
        results, _path = actions.restore(self._write("{}"))
        self.assertFalse(results[0].ok)
        self.assertIn("没有任何要还原的东西", results[0].message)

    def test_unknown_kind_is_reported(self):
        results, _path = actions.restore(self._write('{"ops": [{"kind": "weird", "name": "x"}]}'))
        self.assertFalse(results[0].ok)
        self.assertIn("不认识", results[0].message)


class TestRestoreDoesNotCreateRegistryKeys(unittest.TestCase):
    """还原不能凭空在注册表里造位置。

    备份里写着一个这台机器上不存在的路径时，以前 `_write_value` 会让 Windows
    自动建键，然后报告「已还原开关标记」—— 用户的注册表里就多了一片没人认识的
    位置，而且工具说一切正常。
    """

    def test_missing_subkey_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "ops": [
                            {
                                "kind": "flag",
                                "hive": "HKCU",
                                "subkey": r"Software\No\Such\Place\At\All",
                                "value_name": "x",
                                "existed": True,
                                "old": "020000000000000000000000",
                                "name": "坏位置",
                            }
                        ]
                    },
                    handle,
                )
            results, _path = actions.restore(path)
        self.assertFalse(results[0].ok)
        self.assertIn("没有这个注册表位置", results[0].message)


if __name__ == "__main__":
    unittest.main()
