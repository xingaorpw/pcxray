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
