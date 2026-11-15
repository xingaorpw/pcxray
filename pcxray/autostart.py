"""列出所有开机自启的东西：注册表、启动文件夹、计划任务、自动服务。

四个来源各有各的坑，都实测过：

- **注册表**：`Run` 的值可能是 `REG_SZ` 也可能是 `REG_EXPAND_SZ`（带 `%windir%`），
  32 位程序在 `WOW6432Node` 下另有一套。
- **开关状态**：Windows 自己不删 `Run` 里的值来表示「已禁用」，而是另记一笔
  `Explorer\\StartupApproved`。实测编码是 12 字节：`02…` 启用、`04…` 禁用，
  而且里面可能混着 `REG_SZ` 的脏值（本机就有一条），必须容忍。
- **启动文件夹**：真正的目标是 `.lnk` 快捷方式指向哪里，得解出来才知道是不是
  在临时目录里。
- **计划任务/服务**：只能靠 PowerShell。它失败时必须说「读不到」，
  不能返回空列表 —— 空列表看起来像「系统很干净」。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading

from . import pwsh, safety

__all__ = [
    "Entry",
    "scan",
    "scan_registry",
    "scan_folders",
    "scan_tasks",
    "scan_services",
    "split_exe",
    "expand_vars",
    "decode_approved",
    "approved_state",
]

# (hive 名, 子键, 作用域, 类型, 显示名)
# 这里用字符串而不是 winreg 常量，是为了让这个模块在非 Windows 上也能 import
# （测试要能在 Linux CI 上跑纯逻辑部分）。
RUN_KEYS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", "user", "run", "注册表 · 当前用户 Run"),
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "user", "runonce", "注册表 · 当前用户 RunOnce"),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Run", "machine", "run", "注册表 · 所有用户 Run"),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "machine", "runonce", "注册表 · 所有用户 RunOnce"),
    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "machine", "run32", "注册表 · 所有用户 Run（32 位）"),
)

APPROVED_BASE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

_STARTUP_FOLDERS = (
    ("user", r"Microsoft\Windows\Start Menu\Programs\Startup", "启动文件夹 · 当前用户"),
    ("machine", r"Microsoft\Windows\Start Menu\Programs\Startup", "启动文件夹 · 所有用户"),
)

# 列表排序：先按「关掉它有多大意义」排。RunOnce 是一次性清理命令，
# 排到最后，免得把真正的常驻自启挤下去。
_KIND_ORDER = {"run": 0, "run32": 1, "folder": 2, "task": 3, "service": 4, "runonce": 8}


class Entry:
    """一条自启项。

    `key` 是稳定 id：由「来源 + 作用域 + 名字」算出来，跟命令内容无关 ——
    否则用户改了命令行，界面上那条就从「已禁用」变回「启用」，看不出是同一条。
    """

    __slots__ = (
        "name",
        "kind",
        "source",
        "scope",
        "command",
        "exe",
        "enabled",
        "detail",
        "triggers",
        "protected",
        "note",
        "key",
        "locator",
    )

    def __init__(
        self,
        name: str,
        kind: str,
        source: str,
        scope: str = "user",
        command: str = "",
        exe: str = "",
        enabled: bool = True,
        detail: str = "",
        triggers: str = "",
        protected: bool = False,
        note: str = "",
        locator: dict | None = None,
    ):
        self.name = name
        self.kind = kind
        self.source = source
        self.scope = scope
        self.command = command
        self.exe = exe
        self.enabled = enabled
        self.detail = detail
        self.triggers = triggers
        self.protected = protected
        self.note = note
        self.key = hashlib.sha1(f"{kind}|{scope}|{name}".encode("utf-8")).hexdigest()[:12]
        # 改的时候要用到的定位信息（注册表路径 / 任务路径 / 服务名）。
        # 跟展示用的 detail 分开：detail 是给人看的，locator 是给代码用的，
        # 混在一起早晚会出现「界面上改了显示文案，结果关不掉了」。
        self.locator = locator or {}

    def __repr__(self) -> str:
        state = "启用" if self.enabled else "禁用"
        return f"<Entry {self.name} [{self.source}] {state}>"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "source": self.source,
            "scope": self.scope,
            "command": self.command,
            "exe": self.exe,
            "enabled": self.enabled,
            "detail": self.detail,
            "triggers": self.triggers,
            "protected": self.protected,
            "note": self.note,
            "locator": dict(self.locator),
        }


# ------------------------------------------------------------------ 命令行


_VAR = re.compile(r"%([^%]+)%")


def expand_vars(text: str) -> str:
    """展开 `%windir%` 这类变量。展开不了的保持原样，不猜。"""

    def sub(match: re.Match) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return _VAR.sub(sub, text or "")


def split_exe(command: str) -> str:
    """从命令行里挑出可执行文件的路径。

    处理三种写法：
    - `"C:\\Program Files\\X\\x.exe" --arg`  ← 带引号且路径有空格
    - `C:\\Windows\\system32\\ctfmon.exe`     ← 不带引号
    - `rundll32.exe foo.dll,Entry`            ← 只取第一个 token
    解不出来就返回空串，宁可「不知道」也不要猜一个错的路径出来。
    """
    text = expand_vars((command or "").strip())
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            return text[1:end]
        return text[1:]
    # 没引号：如果整串就是一个存在的文件，直接用它
    if os.path.isfile(text):
        return text
    # 否则按空格切，找到第一个像路径的前缀
    parts = text.split(" ")
    for i in range(len(parts), 0, -1):
        candidate = " ".join(parts[:i])
        if os.path.isfile(candidate):
            return candidate
        if i == 1:
            return candidate
    return parts[0]


# ------------------------------------------------------------------ 注册表


def _hive(name: str):
    import winreg

    return {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}[name]


def _read_values(hive_name: str, subkey: str) -> list[tuple[str, object, int]]:
    """读一个键下的所有值。读不到返回空列表（键不存在是正常情况）。"""
    import winreg

    try:
        key = winreg.OpenKey(_hive(hive_name), subkey)
    except OSError:
        return []
    out: list[tuple[str, object, int]] = []
    try:
        count = winreg.QueryInfoKey(key)[1]
        for i in range(count):
            name, value, kind = winreg.EnumValue(key, i)
            out.append((name, value, kind))
    finally:
        key.Close()
    return out


def decode_approved(value) -> bool | None:
    """把 `StartupApproved` 的值翻译成 启用 / 禁用。

    实测：`02…` 启用、`04…` 禁用（本机 HKLM 里 SecurityHealth、LVAPRCU、
    preinsassoguard 三条都是 04）。`06` 也出现过，按启用算。
    没有记录、或者是 `REG_SZ` 的脏值（本机真有一条空的 GalaxyClient）→ None，
    意思是「系统没记过状态」，也就是默认启用。

    单独拎出来是为了能在非 Windows 上测 —— 判断逻辑不该只有跑在 Windows
    上才能验证。
    """
    if not isinstance(value, (bytes, bytearray)) or not value:
        return None
    return value[0] in (2, 6)


def approved_state(hive_name: str, kind: str, name: str) -> bool | None:
    """读 `StartupApproved` 里的开关状态。没有记录返回 None（等于启用）。"""
    sub = f"{APPROVED_BASE}\\Run32" if kind == "run32" else (
        f"{APPROVED_BASE}\\StartupFolder" if kind == "folder" else f"{APPROVED_BASE}\\Run"
    )
    for candidate in (sub, f"{APPROVED_BASE}\\Run") if kind == "run32" else (sub,):
        for key_name, value, _kind in _read_values(hive_name, candidate):
            if key_name != name:
                continue
            return decode_approved(value)
    return None


def scan_registry() -> list[Entry]:
    """注册表 Run / RunOnce 全部条目。"""
    out: list[Entry] = []
    for hive_name, subkey, scope, kind, label in RUN_KEYS:
        for name, value, vtype in _read_values(hive_name, subkey):
            if not isinstance(value, str):
                continue
            command = expand_vars(value)
            exe = split_exe(command)
            state = approved_state(hive_name, kind, name)
            note = ""
            if kind == "runonce":
                note = "只运行一次：执行完这条会被系统自己删掉，禁用意义不大"
            elif state is None and kind != "runonce":
                note = ""
            out.append(
                Entry(
                    name=name,
                    kind=kind,
                    source=label,
                    scope=scope,
                    command=command,
                    exe=exe,
                    enabled=True if state is None else state,
                    detail=f"{hive_name}\\{subkey}",
                    note=note,
                    protected=False,
                    locator={"hive": hive_name, "subkey": subkey, "value": name},
                )
            )
    return out


# ------------------------------------------------------------------ 启动文件夹


def _startup_dirs() -> list[tuple[str, str, str]]:
    pairs = []
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("ProgramData", "")
    if appdata:
        pairs.append(("user", os.path.join(appdata, _STARTUP_FOLDERS[0][1]), _STARTUP_FOLDERS[0][2]))
    if programdata:
        pairs.append(("machine", os.path.join(programdata, _STARTUP_FOLDERS[1][1]), _STARTUP_FOLDERS[1][2]))
    return pairs


def _resolve_links(paths: list[str]) -> dict[str, tuple[str, str]]:
    """一次 PowerShell 调用解出所有 .lnk 的目标。失败返回空 dict（不是报错）。"""
    if not paths:
        return {}
    quoted = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    script = (
        f"$sh = New-Object -ComObject WScript.Shell; "
        f"Get-Item -LiteralPath {quoted} -ErrorAction SilentlyContinue | ForEach-Object {{ "
        f"$s = $sh.CreateShortcut($_.FullName); "
        f"[pscustomobject]@{{Path=$_.FullName; Target=$s.TargetPath; Args=$s.Arguments}} }} | "
        f"ConvertTo-Json -Compress"
    )
    ok, data, _reason = pwsh.run_json(script, timeout=60)
    if not ok or not data:
        return {}
    rows = data if isinstance(data, list) else [data]
    out: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        out[str(row.get("Path", "")).lower()] = (str(row.get("Target") or ""), str(row.get("Args") or ""))
    return out


def scan_folders() -> list[Entry]:
    """启动文件夹里的条目。.lnk 会尽量解出真实目标。"""
    entries: list[tuple[str, str, str, str]] = []  # scope, label, path, filename
    for scope, folder, label in _startup_dirs():
        if not os.path.isdir(folder):
            continue
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for filename in names:
            if filename.lower() == "desktop.ini":
                continue
            entries.append((scope, label, os.path.join(folder, filename), filename))

    links = [e[2] for e in entries if e[2].lower().endswith(".lnk")]
    resolved = _resolve_links(links)

    out: list[Entry] = []
    for scope, label, path, filename in entries:
        target, args = resolved.get(path.lower(), ("", ""))
        if path.lower().endswith(".lnk"):
            command = (target + (" " + args if args else "")).strip() if target else ""
            exe = target or ""
            detail = f"{path}（快捷方式，指向：{target or '解不出来'}）"
        else:
            command = path
            exe = path
            detail = path
        hive_name = "HKCU" if scope == "user" else "HKLM"
        state = approved_state(hive_name, "folder", filename)
        out.append(
            Entry(
                name=filename,
                kind="folder",
                source=label,
                scope=scope,
                command=command,
                exe=exe,
                enabled=True if state is None else state,
                detail=detail,
                note="" if target or not path.lower().endswith(".lnk") else "快捷方式的目标没解出来，只能看到文件名",
                locator={"hive": hive_name, "subkey": "StartupFolder", "value": filename, "path": path},
            )
        )
    return out


# ------------------------------------------------------------------ 计划任务


def scan_tasks() -> tuple[list[Entry], str]:
    """登录/开机触发的计划任务。返回 (条目, 失败原因)。"""
    script = (
        "Get-ScheduledTask | ForEach-Object { $t = $_; "
        "$trig = @($t.Triggers | Where-Object { $_.CimClass.CimClassName -match 'Logon|Boot' }); "
        "if ($trig.Count -gt 0) { "
        "[pscustomobject]@{ "
        "Name=$t.TaskName; Path=$t.TaskPath; State=[int]$t.State; "
        "Triggers=(($trig | ForEach-Object { $_.CimClass.CimClassName }) -join ','); "
        "Exe=(($t.Actions | ForEach-Object { $_.Execute }) -join ';'); "
        "Args=(($t.Actions | ForEach-Object { $_.Arguments }) -join ' ') } } } | "
        "ConvertTo-Json -Compress"
    )
    ok, data, reason = pwsh.run_json(script, timeout=120)
    if not ok:
        return [], reason or "读不到计划任务"
    rows = data if isinstance(data, list) else ([data] if data else [])
    out: list[Entry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = int(row.get("State") or 0)
        raw_exe = str(row.get("Exe") or "").split(";")[0]
        # 计划任务的 Execute 里经常带 %windir%，不展开就会被判成「文件不存在」，
        # 然后一堆系统自带任务全被标成红色，整张表就没法看了。
        exe = split_exe(raw_exe) if raw_exe else ""
        args = str(row.get("Args") or "")
        path = str(row.get("Path") or "")
        name = str(row.get("Name") or "")
        trigger_names = str(row.get("Triggers") or "")
        readable = "登录" if "Logon" in trigger_names else "开机"
        out.append(
            Entry(
                name=name,
                kind="task",
                source="计划任务",
                # 系统目录下的任务（\Microsoft\Windows\…）属于系统组件
                scope="machine" if path.lower().startswith("\\microsoft\\") else "user",
                command=(exe + (" " + args if args else "")).strip(),
                exe=exe,
                enabled=state != 1,
                detail=f"{path}{name}",
                triggers=readable,
                protected=path.lower().startswith("\\microsoft\\windows\\"),
                locator={"path": path, "name": name},
            )
        )
    return out, ""


# ------------------------------------------------------------------ 服务


def scan_services() -> tuple[list[Entry], str]:
    """开机自动启动的服务。返回 (条目, 失败原因)。"""
    script = (
        "Get-CimInstance Win32_Service -Filter \"StartMode='Auto'\" | "
        "Select-Object Name, DisplayName, State, PathName, StartName | "
        "ConvertTo-Json -Compress"
    )
    ok, data, reason = pwsh.run_json(script, timeout=120)
    if not ok:
        return [], reason or "读不到服务列表"
    rows = data if isinstance(data, list) else ([data] if data else [])
    out: list[Entry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "")
        command = str(row.get("PathName") or "")
        exe = split_exe(command)
        running = str(row.get("State") or "") == "Running"
        out.append(
            Entry(
                name=name,
                kind="service",
                source="自动启动的服务",
                scope="machine",
                command=command,
                exe=exe,
                enabled=True,
                detail=str(row.get("DisplayName") or ""),
                note="正在运行" if running else "已停止（仍会随开机启动）",
                protected=safety.protected_service(name, command),
                locator={"name": name},
            )
        )
    return out, ""


# ------------------------------------------------------------------ 汇总


def scan(include: tuple[str, ...] = ("registry", "folders", "tasks", "services")) -> tuple[list[Entry], list[str]]:
    """扫描全部来源。返回 (条目, 读不到的部分说明)。

    第二部分必须往上抛：某一部分读不到时界面要写「读不到」，不能让用户以为
    那部分真的是空的。

    计划和任务/服务这两块都要起 PowerShell，各约 2.5 秒。串行要 5 秒以上，
    并行压到 3 秒左右 —— 用户等的是「一条命令出结果」，5 秒会以为卡住了。
    """
    entries: list[Entry] = []
    problems: list[str] = []
    if "registry" in include:
        entries.extend(scan_registry())
    if "folders" in include:
        entries.extend(scan_folders())

    wanted = [name for name in ("tasks", "services") if name in include]
    results: dict[str, tuple[list[Entry], str]] = {}
    if wanted:
        threads: list[threading.Thread] = []

        def work(kind: str) -> None:
            try:
                results[kind] = scan_tasks() if kind == "tasks" else scan_services()
            except Exception as exc:  # noqa: BLE001 - 单个来源失败不能拖垮整体
                results[kind] = ([], f"{type(exc).__name__}: {exc}")

        for kind in wanted:
            thread = threading.Thread(target=work, args=(kind,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

    for kind, label in (("tasks", "计划任务"), ("services", "服务")):
        if kind not in results:
            continue
        items, reason = results[kind]
        entries.extend(items)
        if reason:
            problems.append(f"{label}：{reason}")

    entries.sort(key=lambda e: (not e.enabled, _KIND_ORDER.get(e.kind, 9), e.source, e.name.lower()))
    return entries, problems
