"""安全判断：哪些东西能碰、哪些不能碰、哪些值得警惕。

这个模块只做判断，不做修改。放在单独文件里是为了让 `report`（只读展示）
和 `actions`（真的去改）用**同一套**结论 —— 否则会出现「列表里说能关、
点下去报错」这种自相矛盾。
"""

from __future__ import annotations

import os
import re

__all__ = [
    "protected_process",
    "protected_service",
    "exe_of",
    "suspect_path",
    "hidden_args",
    "refuse_reason",
    "needs_force",
    "SUSPECT_HINT",
]

# 这些进程动不得：关掉会蓝屏、丢会话或直接掉网。
PROTECTED_PROCESS = frozenset(
    {
        "system",
        "system idle process",
        "registry",
        "memory compression",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "svchost.exe",
        "fontdrvhost.exe",
        "dwm.exe",
        "explorer.exe",
        "sihost.exe",
        "ctfmon.exe",
        "audiodg.exe",
    }
)

# 这些服务的名字太短太通用，光看名字不够，直接点名保护。
# 用精确集合而不是前缀正则：第一版写成 `^(audio|... )` 的前缀匹配，
# `AudioDeviceVendorTool` 这种第三方服务会被误判成系统组件（真被测试抓到过）。
_PROTECTED_SERVICE = frozenset(
    {
        "wscsvc",
        "windefend",
        "securityhealth",
        "securityhealthservice",
        "mpssvc",
        "bfe",
        "dnscache",
        "dhcp",
        "nsi",
        "eventlog",
        "schedule",
        "wuauserv",
        "trustedinstaller",
        "rpcss",
        "dcomlaunch",
        "plugplay",
        "power",
        "profsvc",
        "themes",
        "audioendpointbuilder",
        "audiosrv",
        "bits",
        "wlan",
        "nlasvc",
        "lanmanworkstation",
        "lanmanserver",
        "gpsvc",
        "cryptsvc",
        "vaultsvc",
    }
)

# 可执行文件落在这些目录下的服务，就是 Windows 自己的一部分。
_SYSTEM_DIRS = ("\\windows\\system32\\", "\\windows\\syswow64\\", "\\windows\\system32\\driversstore\\")

# 这些 exe 是 Windows 的宿主进程，本身就是系统组件。
# 光看路径不够：`svchost.exe -k netsvcs` 这种命令行可能没有目录部分，
# 非管理员下 `Win32_Service.PathName` 还可能整个读不到（CI 上就这么挂的）。
_SYSTEM_HOSTS = frozenset(
    {
        "svchost.exe",
        "services.exe",
        "lsass.exe",
        "wininit.exe",
        "winlogon.exe",
        "smss.exe",
        "csrss.exe",
        "spoolsv.exe",
        "sihost.exe",
        "taskhostw.exe",
        "dllhost.exe",
        "searchindexer.exe",
        "fontdrvhost.exe",
        "wudfhost.exe",
        "audiodg.exe",
    }
)

SUSPECT_HINT = "路径在临时目录或下载目录，正常软件不会把常驻程序放这里"


def protected_process(name: str) -> bool:
    return (name or "").strip().lower() in PROTECTED_PROCESS


def exe_of(command: str) -> str:
    """从命令行里取出 exe 的文件名（小写）。

    `"C:\\a b\\x.exe" --flag` → `x.exe`，`svchost.exe -k netsvcs` → `svchost.exe`。
    """
    raw = (command or "").strip()
    if raw.startswith('"'):
        end = raw.find('"', 1)
        raw = raw[1:end] if end > 0 else raw[1:]
    else:
        raw = raw.split(" ")[0]
    raw = raw.replace("/", "\\").rstrip("\\")
    return raw.rsplit("\\", 1)[-1].lower()


def protected_service(name: str, path: str = "") -> bool:
    """这个服务算不算「系统自带」。

    三条判据，任意一条成立就算：

    1. 名字在点名名单里
    2. 可执行文件是 Windows 的宿主进程（`svchost.exe` 之类）
    3. 可执行文件在 `Windows\\System32` / `SysWOW64` / `DriverStore` 下

    第 3 条比维护一份微软服务名单可靠得多 —— 微软加一个新服务不需要我改代码。

    **读不到路径时不在这里判。** 这个函数只回答「已知的信息说明了什么」；
    「读不到路径该怎么办」是扫描器的策略（见 `autostart.scan_services`），
    放在这里会让「只给名字」的调用莫名其妙全变成 True。
    """
    if (name or "").strip().lower() in _PROTECTED_SERVICE:
        return True
    low = _norm(path)
    if not low:
        return False
    if exe_of(path) in _SYSTEM_HOSTS:
        return True
    return any(fragment in low for fragment in _SYSTEM_DIRS)


def _norm(path: str) -> str:
    return (path or "").replace("/", "\\").lower()


def suspect_path(path: str) -> str:
    """返回可疑原因；没问题返回空串。"""
    low = _norm(path)
    if not low:
        return ""
    if "\\appdata\\local\\temp\\" in low or low.endswith("\\temp"):
        return SUSPECT_HINT
    if "\\windows\\temp\\" in low:
        return SUSPECT_HINT
    if "\\downloads\\" in low:
        return "从下载目录直接运行的程序，通常意味着安装包没装好或是临时下载的东西"
    if "\\$recycle.bin\\" in low or "\\recycler\\" in low:
        return "程序在回收站里"
    drive, _ = os.path.splitdrive(low)
    # 只在 Windows 上有意义；其它盘（U 盘、移动硬盘）上的常驻程序很少是正常的
    if drive and drive.upper() not in ("C:",):
        try:
            from . import winapi

            if winapi.IS_WINDOWS and winapi.drive_type(path) == 2:
                return "程序在可移动磁盘上"
        except (ImportError, OSError, AttributeError):
            pass
    return ""


# 「启动时不显示窗口」这类参数。不是罪证，但值得知道。
_HIDDEN = re.compile(
    r"(?:--silent|-silent|--background|--no-startup-window|--open-at-login|"
    r"--launch-from=loginitem|-windowstyle\s+hidden|--wake|/hb:|--minimized|"
    r"--hidden|/background)",
    re.IGNORECASE,
)


def hidden_args(command: str) -> bool:
    return bool(_HIDDEN.search(command or ""))


def refuse_reason(
    kind: str, scope: str, name: str, protected: bool, admin: bool
) -> str:
    """**绝不改**的理由。返回空串表示没有硬性阻拦。

    注意这里只判「改了就危险」的情况。像服务这种「能改但需要用户明确确认」的，
    不放在这里 —— 那种要放进 `needs_force()`，否则网页上所有第三方服务都会
    变成不可点的死按钮。
    """
    if kind == "runonce":
        return "一次性条目，系统执行后自己就删了，禁用没有意义"
    if protected:
        return "系统自带组件，关掉可能影响开机、网络或安全防护"
    if scope == "machine" and not admin:
        return "改所有用户的范围需要管理员权限"
    return ""


def needs_force(kind: str) -> bool:
    """能改，但要用户再确认一次。目前只有服务。

    服务关错的后果是开不了机，而且不会立刻报错 —— 这种「沉默的严重后果」必须
    让用户多按一次确认键。
    """
    return kind == "service"
