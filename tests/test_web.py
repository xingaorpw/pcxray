"""网页接口测试。真的起一个服务器，用 urllib 打过去。

重点是三件事：页面能出来、报告能出来、**坏请求不能改系统**。
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from pcxray import actions, autostart, report, web, winapi


class FakeReport(report.Report):
    def as_dict(self):
        return {
            "generated": "2026-01-01 00:00:00",
            "admin": False,
            "memory_coverage": "all",
            "logon": 0,
            "boot": 0,
            "ram": {"total": 0, "available": 0},
            "process_count": 0,
            "unreadable_processes": 0,
            "autostart": {"total": 1, "enabled": 1, "disabled": 0, "memory_mb": 0, "items": []},
            "background": [],
            "groups": [],
            "problems": [],
        }


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = FakeReport()
        cls.entry = autostart.Entry(
            name="TestApp",
            kind="run",
            source="测试",
            scope="user",
            locator={"hive": "HKCU", "subkey": "X", "value": "TestApp"},
        )
        cls.patches = [
            mock.patch.object(web, "_snapshot", lambda force=False: cls.report),
            mock.patch.object(web, "_STATE", {"report": cls.report, "entries": [cls.entry]}),
        ]
        for patcher in cls.patches:
            patcher.start()
        cls.server = web.make_server(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for patcher in cls.patches:
            patcher.stop()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=20) as response:
            return response.status, response.read().decode("utf-8")

    def raw_post(self, path, body, headers=None):
        """不自动带 token 的 POST —— 专门用来验「没令牌改不了东西」。"""
        request = urllib.request.Request(
            self.url(path),
            data=body,
            headers=headers if headers is not None else {"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path, payload):
        request = urllib.request.Request(
            self.url(path),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-PCXRAY-Token": self.server.token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class TestPage(ServerCase):
    def test_index_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("pcxray", body)
        self.assertIn("<table>", body)

    def test_version_placeholder_replaced(self):
        _status, body = self.get("/")
        self.assertNotIn("__VERSION__", body)

    def test_index_has_no_absolute_paths(self):
        # 页面里不该出现本机路径（比如备份目录），那是信息泄露
        _status, body = self.get("/")
        self.assertNotIn("C:\\Users", body)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)


class TestReportApi(ServerCase):
    def test_report_json(self):
        status, body = self.get("/api/report")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("autostart", data)
        self.assertIn("background", data)

    def test_refresh_query_accepted(self):
        status, _body = self.get("/api/report?refresh=1")
        self.assertEqual(status, 200)


class TestPostValidation(ServerCase):
    """坏请求一律 400，而且**不能**碰到 actions。"""

    def test_missing_keys(self):
        with mock.patch.object(actions, "disable") as fake:
            status, body = self.post("/api/off", {})
            self.assertEqual(status, 400)
            self.assertIn("至少一条", body["error"])
            fake.assert_not_called()

    def test_empty_keys(self):
        with mock.patch.object(actions, "disable") as fake:
            status, _body = self.post("/api/off", {"keys": []})
            self.assertEqual(status, 400)
            fake.assert_not_called()

    def test_keys_not_a_list(self):
        with mock.patch.object(actions, "disable") as fake:
            status, _body = self.post("/api/off", {"keys": "TestApp"})
            self.assertEqual(status, 400)
            fake.assert_not_called()

    def test_too_many_keys(self):
        with mock.patch.object(actions, "disable") as fake:
            status, body = self.post("/api/off", {"keys": [str(i) for i in range(21)]})
            self.assertEqual(status, 400)
            self.assertIn("20", body["error"])
            fake.assert_not_called()

    def test_unknown_key(self):
        with mock.patch.object(actions, "disable") as fake:
            status, body = self.post("/api/off", {"keys": ["nonexistent"]})
            self.assertEqual(status, 400)
            self.assertIn("找不到", body["error"])
            fake.assert_not_called()

    def test_unknown_path(self):
        status, _body = self.post("/api/nuke", {"keys": []})
        self.assertEqual(status, 404)

    def test_put_not_allowed(self):
        request = urllib.request.Request(self.url("/api/off"), method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=20)
        self.assertEqual(ctx.exception.code, 405)

    def test_malformed_json_does_not_crash(self):
        request = urllib.request.Request(
            self.url("/api/off"),
            data=b"{not json",
            headers={
                "Content-Type": "application/json",
                "X-PCXRAY-Token": self.server.token,
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=20)
        self.assertEqual(ctx.exception.code, 400)


class TestPostAction(ServerCase):
    def test_off_calls_disable_with_the_right_entry(self):
        with mock.patch.object(
            actions, "disable", return_value=([actions.Result("TestApp", True, "ok")], "/tmp/b.json")
        ) as fake:
            status, body = self.post("/api/off", {"keys": [self.entry.key]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["backup"], "/tmp/b.json")
        fake.assert_called_once()
        picked = fake.call_args[0][0]
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0].name, "TestApp")

    def test_on_calls_enable(self):
        with mock.patch.object(
            actions, "enable", return_value=([actions.Result("TestApp", True, "ok")], "")
        ) as fake:
            status, _body = self.post("/api/on", {"keys": [self.entry.key]})
        self.assertEqual(status, 200)
        fake.assert_called_once()

    def test_force_flag_is_forwarded(self):
        with mock.patch.object(
            actions, "disable", return_value=([], "")
        ) as fake:
            self.post("/api/off", {"keys": [self.entry.key], "force": True})
        self.assertTrue(fake.call_args.kwargs.get("force"))

    def test_force_defaults_to_false(self):
        with mock.patch.object(actions, "disable", return_value=([], "")) as fake:
            self.post("/api/off", {"keys": [self.entry.key]})
        self.assertFalse(fake.call_args.kwargs.get("force"))


class TestCsrfProtection(ServerCase):
    """只绑 127.0.0.1 不等于安全：你打开的任何网页都能往这个端口发请求。

    自启项的 key 是 sha1(种类|范围|名字)[:12]，算法公开 —— 攻击者算出
    `run|user|OneDrive` 就能点名关掉它。所以必须有令牌。
    """

    def test_page_carries_a_token(self):
        _status, body = self.get("/")
        self.assertNotIn("__TOKEN__", body)
        self.assertIn(self.server.token, body)

    def test_token_is_not_trivially_guessable(self):
        self.assertGreaterEqual(len(self.server.token), 16)

    def test_two_servers_get_different_tokens(self):
        other = web.make_server(0)
        try:
            self.assertNotEqual(other.token, self.server.token)
        finally:
            other.server_close()

    def test_post_without_token_is_refused(self):
        with mock.patch.object(actions, "disable") as fake:
            status, body = self.raw_post(
                "/api/off",
                json.dumps({"keys": [self.entry.key]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 403)
            self.assertIn("令牌", body["error"])
            fake.assert_not_called()

    def test_post_with_wrong_token_is_refused(self):
        with mock.patch.object(actions, "disable") as fake:
            status, _body = self.raw_post(
                "/api/off",
                json.dumps({"keys": [self.entry.key]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-PCXRAY-Token": "guess"},
            )
            self.assertEqual(status, 403)
            fake.assert_not_called()

    def test_text_plain_body_is_refused(self):
        # 浏览器发跨源请求时，只有 text/plain 能绕过 CORS 预检 —— 这条堵住它
        with mock.patch.object(actions, "disable") as fake:
            status, _body = self.raw_post(
                "/api/off",
                json.dumps({"keys": [self.entry.key]}).encode("utf-8"),
                headers={"Content-Type": "text/plain", "X-PCXRAY-Token": self.server.token},
            )
            self.assertEqual(status, 403)
            fake.assert_not_called()

    def test_cross_origin_is_refused(self):
        with mock.patch.object(actions, "disable") as fake:
            status, _body = self.raw_post(
                "/api/off",
                json.dumps({"keys": [self.entry.key]}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-PCXRAY-Token": self.server.token,
                    "Origin": "http://evil.example",
                },
            )
            self.assertEqual(status, 403)
            fake.assert_not_called()

    def test_same_origin_is_accepted(self):
        with mock.patch.object(
            actions, "disable", return_value=([actions.Result("TestApp", True, "ok")], "")
        ) as fake:
            status, _body = self.raw_post(
                "/api/off",
                json.dumps({"keys": [self.entry.key]}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-PCXRAY-Token": self.server.token,
                    "Origin": self.url(""),
                },
            )
        self.assertEqual(status, 200)
        fake.assert_called_once()

    def test_refresh_parameter_is_parsed_not_substring_matched(self):
        # "refresh=10" 里也含 "refresh=1"，不能因此当成强制刷新
        fake = mock.Mock(return_value=self.report)
        with mock.patch.object(web, "_snapshot", fake):
            self.get("/api/report?refresh=10")
        self.assertFalse(fake.call_args.kwargs.get("force"))

    def test_refresh_1_does_force(self):
        fake = mock.Mock(return_value=self.report)
        with mock.patch.object(web, "_snapshot", fake):
            self.get("/api/report?refresh=1")
        self.assertTrue(fake.call_args.kwargs.get("force"))


class TestSecurityHeaders(ServerCase):
    def test_no_sniff_and_no_frame(self):
        with urllib.request.urlopen(self.url("/"), timeout=20) as response:
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")


class TestBindAddress(unittest.TestCase):
    def test_only_loopback(self):
        server = web.make_server(0)
        try:
            host = server.server_address[0]
            self.assertEqual(host, "127.0.0.1", "只能绑本地，不能对整个局域网开放")
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
