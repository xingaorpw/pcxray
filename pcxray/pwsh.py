"""调 PowerShell 取那些 ctypes 太难拿的数据（计划任务、服务、快捷方式目标）。

只做三件事：找到 powershell、把输出按 UTF-8 解出来、失败时给出**能看懂的原因**。
失败绝不返回空列表 —— 空列表和「真的没有计划任务」在界面上长得一模一样，
用户会以为系统很干净。所以失败一律返回 `(False, None, 原因)`。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

__all__ = ["available", "run_json", "run_text", "ps_quote", "POWERSHELL_HINT"]

POWERSHELL_HINT = "没找到 powershell.exe，这一部分数据读不到（不影响其它部分）"


def ps_quote(value: str) -> str:
    """把任意字符串变成安全的 PowerShell 单引号字面量。

    PowerShell 里单引号字符串的转义是「写两遍」：`o'brien` → `'o''brien'`。
    不转义的话，任务名里一个撇号就能让整段脚本语法错误 —— 而且报出来的是
    「PowerShell 输出不是合法 JSON」这种跟真实原因毫无关系的话。
    """
    return "'" + str(value).replace("'", "''") + "'"


def _exe() -> str | None:
    found = shutil.which("powershell") or shutil.which("powershell.exe")
    if found:
        return found
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def available() -> bool:
    return _exe() is not None


# 输出编码必须在脚本里强制成 UTF-8：中文 Windows 的 PowerShell 默认按 GBK 输出，
# 直接 decode("utf-8") 会把中文任务名变成乱码。
_PREAMBLE = (
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
)


def _run(script: str, timeout: float) -> tuple[int, bytes, bytes, str]:
    """跑一段 PowerShell。返回 (退出码, stdout, stderr, 失败原因)。"""
    exe = _exe()
    if not exe:
        return -1, b"", b"", POWERSHELL_HINT
    cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PREAMBLE + script]
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return -1, b"", b"", f"PowerShell 超过 {timeout:.0f} 秒没返回，已放弃这一部分"
    except OSError as exc:
        return -1, b"", b"", f"启动 PowerShell 失败：{exc}"
    return done.returncode, done.stdout, done.stderr, ""


def run_text(script: str, timeout: float = 60.0) -> tuple[bool, str, str]:
    """跑一段 PowerShell，只按退出码判断成败，原样返回 stdout。

    **改系统的动作用这个，不要用 `run_json`。** 第一版的 `off <计划任务>` 让
    脚本输出 `'ok'` 再交给 `run_json` 解析 —— `ok` 不是 JSON，于是每次都返回
    「PowerShell 输出不是合法 JSON：ok」。而任务其实已经被禁用了：**动作成功
    却报失败**，用户会以为没生效然后反复重试。这个 bug 是真机跑出来才发现的。
    """
    code, out, err, reason = _run(script, timeout)
    if reason:
        return False, "", reason
    text = _text(out).strip()
    if code != 0:
        lines = _text(err).strip().splitlines()
        return False, text, (lines[-1] if lines else f"PowerShell 退出码 {code}")[:300]
    return True, text, ""


def run_json(script: str, timeout: float = 90.0) -> tuple[bool, object, str]:
    """跑一段 PowerShell，把 stdout 当 JSON 解析。

    返回 `(成功?, 数据, 失败原因)`。成功但没有输出时数据是 None。
    """
    code, out, err, reason = _run(script, timeout)
    if reason:
        return False, None, reason

    raw = out
    if not raw.strip():
        if code != 0:
            lines = _text(err).strip().splitlines()
            return False, None, (lines[-1] if lines else f"PowerShell 退出码 {code}")[:300]
        return True, None, ""
    text = _text(raw).strip()
    try:
        return True, json.loads(text), ""
    except json.JSONDecodeError:
        # 有的命令会先打印一行警告再输出 JSON
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    return True, json.loads(line), ""
                except json.JSONDecodeError:
                    continue
        return False, None, f"PowerShell 输出不是合法 JSON：{text[:200]}"


def _text(raw: bytes) -> str:
    """优先 UTF-8（我们已经在脚本里强制了），退不动就试系统代码页。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("gbk", "mbcs" if sys.platform == "win32" else "latin-1"):
        try:
            return raw.decode(enc, "replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")
