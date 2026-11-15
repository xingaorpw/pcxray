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

from pcxray import actions, autostart


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
        for name in ("A", "B", "C"):
            actions._save_backup([{"kind": "flag", "name": name}])
        listed = actions.list_backups()
        self.assertEqual(listed, sorted(listed, reverse=True))

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
        self.assertIn("已写入", results[0].message)

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
