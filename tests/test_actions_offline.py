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


if __name__ == "__main__":
    unittest.main()
