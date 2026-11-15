"""PowerShell 包装层。

重点是 `ps_quote` —— 一个撇号就能让整段脚本语法错误，而报出来的错跟真实
原因毫无关系（`TerminatorExpectedAtEndOfString`）。这个是真机上跑出来的。
"""

from __future__ import annotations

import unittest
from unittest import mock

from pcxray import pwsh


class TestQuote(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(pwsh.ps_quote("abc"), "'abc'")

    def test_single_quote_doubled(self):
        self.assertEqual(pwsh.ps_quote("o'brien"), "'o''brien'")

    def test_two_quotes(self):
        self.assertEqual(pwsh.ps_quote("a'b'c"), "'a''b''c'")

    def test_empty(self):
        self.assertEqual(pwsh.ps_quote(""), "''")

    def test_chinese_and_spaces(self):
        self.assertEqual(pwsh.ps_quote("张 啸宇"), "'张 啸宇'")

    def test_dollar_sign_is_not_expanded(self):
        # 单引号里 $ 不会被展开，这正是用单引号的原因
        self.assertEqual(pwsh.ps_quote("$env:Path"), "'$env:Path'")


class TestUnavailable(unittest.TestCase):
    """没有 PowerShell 时，所有调用都必须给出明确原因，而不是空结果。"""

    def test_run_text_reports_missing_powershell(self):
        with mock.patch.object(pwsh, "_exe", lambda: None):
            ok, out, reason = pwsh.run_text("Get-Date")
        self.assertFalse(ok)
        self.assertEqual(out, "")
        self.assertIn("powershell", reason.lower())

    def test_run_json_reports_missing_powershell(self):
        with mock.patch.object(pwsh, "_exe", lambda: None):
            ok, data, reason = pwsh.run_json("Get-Date")
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("powershell", reason.lower())


@unittest.skipUnless(pwsh.available(), "这台机器没有 PowerShell")
class TestFailurePaths(unittest.TestCase):
    """PowerShell 出各种问题时要给出能看懂的原因，不能返回空列表。

    空列表和「真的没有计划任务」在界面上长得一模一样，用户会以为系统很干净。
    这几条路径以前只有真机才碰得到，等于没测。
    """

    def setUp(self):
        patcher = mock.patch.object(pwsh, "_exe", return_value="powershell.exe")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_with(self, timeout=90, **kwargs):
        with mock.patch.object(pwsh.subprocess, "run", **kwargs):
            return pwsh.run_json("Get-Thing", timeout=timeout)

    def test_timeout_is_explained(self):
        ok, data, reason = self._run_with(
            timeout=30, side_effect=pwsh.subprocess.TimeoutExpired("x", 30)
        )
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("没返回", reason)
        self.assertIn("30", reason, "提示里要写清楚等了多少秒")

    def test_start_failure_is_explained(self):
        ok, _data, reason = self._run_with(side_effect=OSError("拒绝访问"))
        self.assertFalse(ok)
        self.assertIn("启动 PowerShell 失败", reason)

    def test_nonzero_exit_uses_stderr(self):
        done = mock.Mock(returncode=5, stdout=b"", stderr="拒绝访问".encode("utf-8"))
        ok, _data, reason = self._run_with(return_value=done)
        self.assertFalse(ok)
        self.assertIn("拒绝访问", reason)

    def test_empty_output_and_zero_exit_is_success_with_none(self):
        done = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        ok, data, reason = self._run_with(return_value=done)
        self.assertTrue(ok)
        self.assertIsNone(data)
        self.assertEqual(reason, "")

    def test_warning_line_before_json_is_skipped(self):
        done = mock.Mock(
            returncode=0,
            stdout=b"WARNING: something\n[{\"Name\": \"X\"}]\n",
            stderr=b"",
        )
        ok, data, _reason = self._run_with(return_value=done)
        self.assertTrue(ok)
        self.assertEqual(data, [{"Name": "X"}])

    def test_gbk_output_is_decoded(self):
        # 中文 Windows 的 PowerShell 可能按 GBK 吐字节
        done = mock.Mock(returncode=0, stdout='{"Name": "微信"}'.encode("gbk"), stderr=b"")
        ok, data, _reason = self._run_with(return_value=done)
        self.assertTrue(ok, data)
        self.assertEqual(data["Name"], "微信")

    def test_long_stderr_is_truncated(self):
        done = mock.Mock(returncode=1, stdout=b"", stderr=("错误" * 400).encode("utf-8"))
        with mock.patch.object(pwsh.subprocess, "run", return_value=done):
            ok, _text, reason = pwsh.run_text("x")
        self.assertFalse(ok)
        self.assertLessEqual(len(reason), 300)

    def test_missing_powershell_does_not_look_like_empty(self):
        with mock.patch.object(pwsh, "_exe", return_value=None):
            ok, data, reason = pwsh.run_json("x")
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("没找到 powershell", reason)


class TestAgainstRealPowerShell(unittest.TestCase):
    def test_run_text_ok(self):
        ok, out, reason = pwsh.run_text("Write-Output 'hello'", timeout=30)
        self.assertTrue(ok, reason)
        self.assertEqual(out, "hello")

    def test_run_text_failure_has_reason(self):
        ok, _out, reason = pwsh.run_text("throw 'boom'", timeout=30)
        self.assertFalse(ok)
        self.assertIn("boom", reason)

    def test_run_text_nonzero_exit(self):
        ok, _out, reason = pwsh.run_text("exit 3", timeout=30)
        self.assertFalse(ok)
        self.assertIn("3", reason)

    def test_run_json_parses(self):
        ok, data, reason = pwsh.run_json("@{a=1} | ConvertTo-Json -Compress", timeout=30)
        self.assertTrue(ok, reason)
        self.assertEqual(data, {"a": 1})

    def test_run_json_rejects_plain_text(self):
        # 这就是计划任务那条 bug 的根因：输出 'ok' 不是 JSON
        ok, data, reason = pwsh.run_json("'ok'", timeout=30)
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("不是合法 JSON", reason)

    def test_quote_survives_a_real_round_trip(self):
        # 撇号不转义 → TerminatorExpectedAtEndOfString；转义后正常
        quoted = pwsh.ps_quote("o'brien")
        ok, out, reason = pwsh.run_text("Write-Output " + quoted, timeout=30)
        self.assertTrue(ok, reason)
        self.assertEqual(out, "o'brien")

    def test_unquoted_apostrophe_really_breaks(self):
        ok, _out, _reason = pwsh.run_text("Write-Output 'o'brien'", timeout=30)
        self.assertFalse(ok, "没转义的撇号居然没报错，说明这条测试没测到东西")

    def test_chinese_round_trip(self):
        ok, out, reason = pwsh.run_text("Write-Output '中文测试'", timeout=30)
        self.assertTrue(ok, reason)
        self.assertEqual(out, "中文测试")


if __name__ == "__main__":
    unittest.main()
