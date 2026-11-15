"""网页接口测试。真的起一个服务器，用 urllib 打过去。

重点是三件事：页面能出来、报告能出来、**坏请求不能改系统**。
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from pcxray import actions, autostart, report, web, winapi


class TestServeStartup(unittest.TestCase):
    """`serve()` 是用户真正跑的那条路（`pcxray web`），以前只有集成测试碰过。

    它要在三种情况下都给出人话：起不来端口、正常启动、Ctrl+C 退出。
    """

    class _FakeServer:
        def __init__(self, address=("127.0.0.1", 51234), interrupt=False):
            self.server_address = address
            self._interrupt = interrupt
            self.closed = False

        def serve_forever(self):
            if self._interrupt:
                raise KeyboardInterrupt
            raise AssertionError("测试不该真的进服务循环")

        def server_close(self):
            self.closed = True

    def test_non_windows_exits_2(self):
        err = io.StringIO()
        with mock.patch.object(web.winapi, "IS_WINDOWS", False), contextlib.redirect_stderr(err):
            code = web.serve(port=0, open_browser=False)
        self.assertEqual(code, 2)
        self.assertIn("只能在 Windows 上运行", err.getvalue())

    def test_port_failure_goes_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(web.winapi, "IS_WINDOWS", True), mock.patch.object(
            web, "make_server", side_effect=OSError("端口被占用")
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = web.serve(port=1234, open_browser=False)
        self.assertEqual(code, 1)
        self.assertIn("端口 1234 起不来", err.getvalue())
        self.assertEqual(out.getvalue(), "", "错误不能混进正常输出里")

    def test_ctrl_c_closes_the_server(self):
        server = self._FakeServer(interrupt=True)
        out = io.StringIO()
        with mock.patch.object(web.winapi, "IS_WINDOWS", True), mock.patch.object(
            web, "make_server", return_value=server
        ), mock.patch.object(web, "_snapshot", return_value=report.Report()), contextlib.redirect_stdout(out):
            code = web.serve(port=0, open_browser=False)
        self.assertEqual(code, 0)
        self.assertTrue(server.closed, "退出时必须 server_close")
        self.assertIn("51234", out.getvalue(), "要把真实端口打出来")

    def test_unexpected_error_still_closes_the_server(self):
        class Boom(self._FakeServer):
            def serve_forever(self):
                raise RuntimeError("网卡没了")

        server = Boom()
        with mock.patch.object(web.winapi, "IS_WINDOWS", True), mock.patch.object(
            web, "make_server", return_value=server
        ), mock.patch.object(web, "_snapshot", return_value=report.Report()), contextlib.redirect_stdout(
            io.StringIO()
        ):
            with self.assertRaises(RuntimeError):
                web.serve(port=0, open_browser=False)
        self.assertTrue(server.closed, "崩了也要关，否则端口一直占着")


class TestReadBodyLimits(unittest.TestCase):
    """POST 请求体的三道闸：长度不是数字、太长、不是 JSON。

    这三条是外部输入的第一道关，以前没测过。
    """

    def _handler(self, headers, raw=b""):
        handler = web.Handler.__new__(web.Handler)
        handler.headers = headers
        handler.rfile = io.BytesIO(raw)
        return handler

    def test_non_numeric_content_length_is_empty(self):
        handler = self._handler({"Content-Length": "abc"})
        self.assertEqual(handler._read_body(), {})

    def test_missing_content_length_is_empty(self):
        self.assertEqual(self._handler({})._read_body(), {})

    def test_oversized_body_is_refused_without_reading(self):
        handler = self._handler({"Content-Length": str(65537)}, raw=b"{}")
        self.assertEqual(handler._read_body(), {})

    def test_non_json_body_is_empty(self):
        handler = self._handler({"Content-Length": "5"}, raw=b"hello")
        self.assertEqual(handler._read_body(), {})

    def test_json_array_is_not_a_dict(self):
        handler = self._handler({"Content-Length": "2"}, raw=b"[]")
        self.assertEqual(handler._read_body(), {})

    def test_normal_body_is_parsed(self):
        handler = self._handler({"Content-Length": "15"}, raw=b'{"names":["a"]}')
        self.assertEqual(handler._read_body(), {"names": ["a"]})


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


class TestIdleConnectionsDoNotPileUp(ServerCase):
    """连上不说话的客户端不能永远占着一条线程。

    `BaseHTTPRequestHandler.timeout` 默认是 None —— 也就是永远等下去。浏览器
    关标签页、进程被强杀都会留下半死的连接，每一条都占一条线程，而且永远不
    释放。实测过：开 10 个空连接，8 秒后一个都没被服务端关掉。
    """

    def test_idle_socket_is_closed_once_the_timeout_passes(self):
        sockets = []
        try:
            with mock.patch.object(web.Handler, "timeout", 1):
                for _ in range(3):
                    sockets.append(socket.create_connection(("127.0.0.1", self.port), timeout=5))
                # 什么都不发，等服务端自己关
                deadline = time.time() + 5
                closed = 0
                while time.time() < deadline and closed < len(sockets):
                    closed = 0
                    for sock in sockets:
                        sock.settimeout(0.2)
                        try:
                            if sock.recv(1) == b"":
                                closed += 1
                        except socket.timeout:
                            pass
                        except OSError:
                            closed += 1
                    if closed < len(sockets):
                        time.sleep(0.2)
            self.assertEqual(closed, len(sockets), f"只有 {closed}/{len(sockets)} 条空闲连接被关掉了")
        finally:
            for sock in sockets:
                try:
                    sock.close()
                except OSError:
                    pass

    def test_handler_has_a_finite_timeout(self):
        # 真值不许是 None：None 就是「永远等」
        self.assertIsNotNone(web.Handler.timeout)
        self.assertGreater(web.Handler.timeout, 0)
        self.assertLessEqual(web.Handler.timeout, 120)


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


class TestScanFailureStillAnswers(ServerCase):
    """扫描炸了也必须回 JSON。

    以前 `_snapshot` 抛异常会一路冒到 socketserver，连接被直接掐断 —— 浏览器
    那边只看到 `TypeError: Failed to fetch`，用户完全不知道发生了什么。接口的
    契约是「永远回 JSON」，异常也得翻译成 JSON。
    """

    def test_scan_exception_becomes_500_json(self):
        with mock.patch.object(web, "_snapshot", side_effect=RuntimeError("扫描时炸了")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.url("/api/report"), timeout=20)
        self.assertEqual(caught.exception.code, 500)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertFalse(body["ok"])
        self.assertIn("扫描时出错了", body["error"])
        self.assertIn("扫描时炸了", body["error"], "原因要照抄，不能吞掉")

    def test_action_exception_becomes_500_json(self):
        with mock.patch.object(web.Handler, "_apply", side_effect=RuntimeError("改的时候炸了")):
            request = urllib.request.Request(
                self.url("/api/off"),
                data=json.dumps({"keys": ["TestApp"]}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-PCXRAY-Token": self.server.token,
                    "Origin": f"http://127.0.0.1:{self.port}",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=20)
        self.assertEqual(caught.exception.code, 500)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("改的时候出错了", body["error"])


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


class TestNoUnescapedInterpolation(unittest.TestCase):
    """页面里凡是往 HTML 里塞「来自系统」的字符串，都必须过 `esc()`。

    自启项的名字、命令行、发行商、路径全都来自本机注册表和 exe 版本信息 ——
    一条自启项的名字里带 `<img onerror=...>` 完全可能。这里做静态检查：
    模板里每个 `${...}` 如果引用了这些字段，就必须包着 esc。
    """

    # 这些标识符代表「可能来自外部的字符串」
    TAINTED = ("i.name", "i.command", "i.detail", "i.source", "i.company", "i.refuse",
               "p.name", "p.path", "DATA.problems")
    # 这个局部变量是自己用 esc 拼出来的，见下面 test_flags_are_escaped_where_built
    PRE_ESCAPED = ("flags",)

    def _interpolations(self):
        import re

        return re.findall(r"\$\{([^{}]*)\}", web._PAGE)

    def test_every_tainted_field_is_escaped(self):
        import re

        offenders = []
        for expr in self._interpolations():
            if expr.strip() in self.PRE_ESCAPED:
                continue
            if any(word in expr for word in self.TAINTED):
                # 允许 esc(x) 或 esc(x)||"..." 这类，核心是必须有 esc(
                if not re.search(r"\besc\(", expr):
                    offenders.append(expr)
        self.assertEqual(offenders, [], f"这些插值没过 esc()：{offenders}")

    def test_flags_are_escaped_where_built(self):
        # `${flags}` 本身不再转义，所以拼接它的那一处必须转义
        import re

        block = web._PAGE[web._PAGE.index("const flags=") :]
        block = block[: block.index(".join(")]
        self.assertIn("esc(f)", block)

    def test_esc_itself_neutralises_a_payload(self):
        # esc 的实现也一起钉住：五个字符都要转
        import re

        match = re.search(r"const esc=(.*?);\n", web._PAGE)
        self.assertIsNotNone(match)
        body = match.group(1)
        for ch in ("&", "<", ">", '"', "'"):
            with self.subTest(char=ch):
                self.assertIn(ch, body)

    def test_no_inline_onclick_with_data(self):
        # 把 key 拼进 onclick 属性里是上一版的写法，改成 data-* + 事件委托了
        self.assertNotIn('onclick="toggle(', web._PAGE)


class TestPortHandling(unittest.TestCase):
    """端口被占时要说人话，而且别白扫一遍。

    以前 `serve()` 先扫描（约 3 秒）再绑端口 —— 端口冲突时用户等了三秒才被告知
    「起不来」，而且扫描结果直接扔掉。
    """

    def test_reuse_address_is_off_on_windows(self):
        # Windows 上 SO_REUSEADDR 允许别的进程抢绑同一个端口。这个接口能关自启项，
        # 端口被抢走就等于被人替你做决定。
        if winapi.IS_WINDOWS:
            self.assertFalse(web._Server.allow_reuse_address)
        else:
            self.assertTrue(web._Server.allow_reuse_address)

    def test_occupied_port_fails_before_scanning(self):
        import socket

        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        holder.listen(1)
        self.addCleanup(holder.close)
        scanned = []
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(web, "_snapshot", lambda **kw: scanned.append(1) or None):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = web.serve(port=port, open_browser=False)
        # 报错信息必须提到「端口」和换一个的出路，不能只丢个 WinError 给用户
        # 而且要写在 stderr：脚本重定向 stdout 时，错误混进正常输出里就看不见了
        self.assertIn("端口", err.getvalue())
        self.assertIn("--port 0", err.getvalue())
        self.assertEqual(out.getvalue(), "", "失败的时候 stdout 不该有东西")
        self.assertEqual(code, 1)
        self.assertEqual(scanned, [], "端口都起不来，不该先去扫描")

    def test_daemon_threads_so_one_stuck_request_does_not_block_exit(self):
        self.assertTrue(web._Server.daemon_threads)


class TestBindAddress(unittest.TestCase):
    def test_only_loopback(self):
        server = web.make_server(0)
        try:
            host = server.server_address[0]
            self.assertEqual(host, "127.0.0.1", "只能绑本地，不能对整个局域网开放")
        finally:
            server.server_close()


class TestMovedEntryUsesMoveMode(unittest.TestCase):
    """界面上没有模式开关，用户点「启用」就该办成。

    被 --mode move 挪走的条目只能靠挪键放回去；如果一律用标记模式，点「启用」
    只会得到一句「要用 pcxray on 名字 --mode move」—— 在图形界面里等于死路。
    """

    def setUp(self):
        self.server = web.make_server(port=0)
        self.addCleanup(self.server.server_close)
        self.token = self.server.token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def test_moved_entry_gets_move_mode_and_plain_entry_gets_flag_mode(self):
        moved = autostart.Entry(
            name="挪走的",
            kind="run",
            source="注册表 · 当前用户 Run",
            scope="user",
            enabled=False,
            locator={"hive": "HKCU", "subkey": "X-pcxray-disabled", "value": "挪走的", "moved": True},
        )
        plain = autostart.Entry(
            name="正常的",
            kind="run",
            source="注册表 · 当前用户 Run",
            scope="user",
            locator={"hive": "HKCU", "subkey": "X", "value": "正常的"},
        )
        calls = []

        def fake_enable(items, force=False, mode=actions.MODE_FLAG):
            calls.append(([e.name for e in items], mode))
            return [actions.Result(e.name, True, "好") for e in items], ""

        with mock.patch.dict(web._STATE, {"entries": [moved, plain]}), mock.patch.object(
            web.actions, "enable", side_effect=fake_enable
        ):
            status, payload = self._post("/api/on", [moved.key, plain.key])

        self.assertEqual(status, 200, payload)
        self.assertEqual(
            calls,
            [(["正常的"], actions.MODE_FLAG), (["挪走的"], actions.MODE_MOVE)],
            "挪走的条目必须走 move 模式，不然界面里点不动",
        )

    def _post(self, path, keys):
        port = self.server.server_address[1]
        body = json.dumps({"keys": keys}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body,
            headers={
                "X-PCXRAY-Token": self.token,
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class TestStateChangeIsSerialised(unittest.TestCase):
    """改状态和重扫不能交错。

    以前 POST 直接改 `_STATE`，不加锁：一次并发的刷新可能正好在写之前扫完，
    把旧结果写进缓存，而 POST 的作废标记在那之前就被覆盖了 —— 用户点了「关掉」，
    刷新后那一行还是「启用」。
    """

    def setUp(self):
        self.server = web.make_server(port=0)
        self.addCleanup(self.server.server_close)
        self.token = self.server.token
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)

    def test_post_waits_for_the_rescan_to_finish(self):
        started = threading.Event()
        release = threading.Event()
        real_scan = autostart.scan

        def slow_scan(*args, **kwargs):
            started.set()
            release.wait(5)
            return real_scan(*args, **kwargs)

        with mock.patch.object(autostart, "scan", slow_scan):
            rebuild = threading.Thread(target=lambda: web._snapshot(force=True), daemon=True)
            rebuild.start()
            self.assertTrue(started.wait(5), "重扫没开始")

            done = threading.Event()

            def post():
                self._post()
                done.set()

            poster = threading.Thread(target=post, daemon=True)
            poster.start()
            # 重扫还占着锁，POST 必须等着 —— 等不到说明没锁住
            self.assertFalse(done.wait(0.5), "重扫还在跑，POST 就已经改完状态了")
            release.set()
            self.assertTrue(done.wait(10), "放行之后 POST 还是没完成")
            rebuild.join(5)

    def _post(self):
        port = self.server.server_address[1]
        body = json.dumps({"keys": ["不存在的键"]}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/off",
            data=body,
            headers={
                "X-PCXRAY-Token": self.token,
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10).read()
        except urllib.error.HTTPError:
            pass  # 400 也是「处理完了」


if __name__ == "__main__":
    unittest.main()
