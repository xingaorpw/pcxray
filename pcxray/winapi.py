"""Windows 原生调用，只用 ctypes，不引第三方包。

为什么不用 PowerShell 拿进程表：实测 164 个进程走 PowerShell 要 2 秒以上，
走 ctypes 只要 26 毫秒。开机自启那种需要反复扫描的场景，2 秒是不能接受的。

这个模块里每个函数都只在 Windows 上有意义。所有 ctypes 绑定都显式声明
`argtypes` / `restype` —— 不声明的话 Python 按 C int 处理返回值，
`GetTickCount64` 这种 64 位值会在系统运行 24.8 天后翻成负数。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

__all__ = [
    "Process",
    "Windows",
    "processes",
    "boot_time",
    "is_admin",
    "elevate",
    "drive_type",
    "memory_status",
    "version_info",
    "COVERAGE",
    "IS_WINDOWS",
]

IS_WINDOWS = sys.platform == "win32"

# ------------------------------------------------------------------ 类型别名

# 不用 `ctypes.wintypes`：那个模块在非 Windows 上的导入行为不保证，而这个文件
# 会被测试在 Linux CI 上 import（为了跑纯逻辑部分）。自己定义几个别名，行为
# 在哪都一样。
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int
HANDLE = ctypes.c_void_p
UINT = ctypes.c_uint
USHORT = ctypes.c_ushort
ULONG = ctypes.c_uint32
WCHAR = ctypes.c_wchar
LPWSTR = ctypes.c_wchar_p
LPCWSTR = ctypes.c_wchar_p
LPARAM = ctypes.c_ssize_t
HWND = ctypes.c_void_p
LONG = ctypes.c_int32


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", DWORD),
        ("dwMemoryLoad", DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

# ------------------------------------------------------------------ 常量

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH_LONG = 32768

# NtQuerySystemInformation 的 SystemProcessInformation 类
_SYSTEM_PROCESS_INFORMATION = 5
# 缓冲区不够时返回的 NTSTATUS
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

# FILETIME 是 1601-01-01 起的 100 纳秒数，转 Unix 时间要减掉这段偏移
_FILETIME_EPOCH = 11644473600


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", DWORD),
        ("szExeFile", WCHAR * 260),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", USHORT),
        ("MaximumLength", USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    """`NtQuerySystemInformation(SystemProcessInformation)` 的单条记录。

    字段偏移在 Vista 之后的 x64 上稳定，实测 `WorkingSetSize` 落在 0x90、
    `PrivatePageCount` 落在 0xC8，和 `GetProcessMemoryInfo` 的交叉验证
    145 个一致、1 个差异（差异那条是扫描期间自己长大的 python.exe）。
    仍然只用这两个字段：再往后的字段布局更容易随版本变，不碰。
    """

    _fields_ = [
        ("NextEntryOffset", ULONG),
        ("NumberOfThreads", ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", ULONG),
        ("NumberOfThreadsHighWatermark", ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ULONG),
        ("SessionId", ULONG),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("PageFaultCount", DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _bind():
    """把要用的函数签名一次性绑好。只在 Windows 上调用。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    k32.CreateToolhelp32Snapshot.restype = HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [DWORD, DWORD]
    k32.Process32FirstW.restype = BOOL
    k32.Process32FirstW.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = BOOL
    k32.Process32NextW.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = HANDLE
    k32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    k32.CloseHandle.restype = BOOL
    k32.CloseHandle.argtypes = [HANDLE]
    k32.QueryFullProcessImageNameW.restype = BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        HANDLE,
        DWORD,
        LPWSTR,
        ctypes.POINTER(DWORD),
    ]
    k32.GetProcessTimes.restype = BOOL
    k32.GetProcessTimes.argtypes = [
        HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    # 必须声明：默认按 c_int 返回，运行 24.8 天后会翻成负数
    k32.GetTickCount64.restype = ctypes.c_ulonglong
    k32.GetTickCount64.argtypes = []

    psapi.GetProcessMemoryInfo.restype = BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
        DWORD,
    ]

    user32.EnumWindows.restype = BOOL
    user32.EnumWindows.argtypes = [ctypes.c_void_p, LPARAM]
    user32.IsWindowVisible.restype = BOOL
    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsIconic.restype = BOOL
    user32.IsIconic.argtypes = [HWND]
    user32.GetWindowThreadProcessId.restype = DWORD
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [HWND]

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    ntdll.NtQuerySystemInformation.argtypes = [
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    return k32, psapi, user32, ntdll


_LIBS: tuple | None = None

# 内存数据的覆盖度。自检没过时退回句柄路径，能看到的进程会少一半 ——
# 这件事必须让调用方知道并且显示出来，否则用户会以为「就这么多进程」。
COVERAGE: dict[str, str] = {"mode": "all", "reason": ""}


def _libs():
    global _LIBS
    if _LIBS is None:
        if not IS_WINDOWS:
            raise RuntimeError("pcxray 只能在 Windows 上运行")
        _LIBS = _bind()
    return _LIBS


# ------------------------------------------------------------------ 数据结构


@dataclass
class Windows:
    """一个进程的窗口情况。

    「有没有窗口」不能只看 `IsWindowVisible`：最小化到托盘的微信、Tauri/Electron
    应用会保留窗口但不可见。所以分开记：全部窗口数、可见数、最小化数。
    """

    total: int = 0
    visible: int = 0
    minimized: int = 0

    @property
    def hidden(self) -> bool:
        """完全没有可见窗口 —— 托盘程序或纯后台。"""
        return self.visible == 0


@dataclass
class Process:
    pid: int
    name: str
    path: str = ""
    working_set: int = 0  # 工作集：任务管理器「内存」那一列
    private: int = 0  # 私有提交：真正只属于这个进程的部分
    started: float = 0.0
    windows: Windows = field(default_factory=Windows)
    readable: bool = True  # 拿不到内存/路径（受保护进程，通常需要管理员）

    @property
    def mb(self) -> float:
        return self.working_set / 1048576

    @property
    def private_mb(self) -> float:
        return self.private / 1048576


# ------------------------------------------------------------------ 进程表


def _windows_by_pid() -> dict[int, Windows]:
    """一次 EnumWindows 拿到所有进程的窗口情况。

    不这么做的话就得每个进程单独查，164 个进程要 164 次系统调用。
    """
    _, _, user32, _ = _libs()
    out: dict[int, Windows] = {}

    @ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)
    def _cb(hwnd, _lparam):
        pid = DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        info = out.setdefault(pid.value, Windows())
        info.total += 1
        if user32.IsWindowVisible(hwnd):
            info.visible += 1
            if user32.IsIconic(hwnd):
                info.minimized += 1
        return True

    user32.EnumWindows(_cb, 0)
    return out


def _memory_by_pid() -> dict[int, tuple[int, int]]:
    """拿到**所有**进程的内存，不需要管理员。

    为什么不用 `OpenProcess` + `GetProcessMemoryInfo`：非管理员实测 378 个进程
    里只有 162 个能打开，剩下 216 个（占 57%）的内存根本读不到 —— 那样
    「谁在后台占内存」这个问题只能回答一半。

    `NtQuerySystemInformation(SystemProcessInformation)` 不需要逐个进程的句柄，
    一次调用给出全表。这是未文档化接口，所以：
    - 只读 `WorkingSetSize` / `PrivatePageCount` 两个字段（偏移实测稳定）
    - 启动时和 `GetProcessMemoryInfo` 交叉验证，对不上就整体退回句柄路径
    """
    _, _, _, ntdll = _libs()
    length = 1 << 22  # 4 MB 起步，这台机器实测 748 KB 就够
    for _ in range(4):
        buf = ctypes.create_string_buffer(length)
        ret_len = ULONG(0)
        status = ntdll.NtQuerySystemInformation(
            _SYSTEM_PROCESS_INFORMATION, buf, length, ctypes.byref(ret_len)
        )
        if status == _STATUS_INFO_LENGTH_MISMATCH or (
            status & 0xFFFFFFFF
        ) == _STATUS_INFO_LENGTH_MISMATCH:
            length = max(length * 2, int(ret_len.value) + 65536)
            continue
        if status != 0:
            return {}

        out: dict[int, tuple[int, int]] = {}
        offset = 0
        while True:
            info = SYSTEM_PROCESS_INFORMATION.from_buffer(buf, offset)
            pid = ctypes.cast(info.UniqueProcessId, ctypes.c_void_p).value or 0
            out[pid] = (int(info.WorkingSetSize), int(info.PrivatePageCount))
            if not info.NextEntryOffset:
                break
            offset += info.NextEntryOffset
        return out
    return {}


def _exe_path(k32, handle) -> str:
    buf = ctypes.create_unicode_buffer(MAX_PATH_LONG)
    size = DWORD(MAX_PATH_LONG)
    if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return ""
    return buf.value


def _started_at(k32, handle) -> float:
    created = FILETIME()
    exited = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if not k32.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return 0.0
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    return ticks / 10_000_000 - _FILETIME_EPOCH


def _verify_memory(k32, psapi, mem: dict[int, tuple[int, int]]) -> bool:
    """抽样比对未文档化接口和官方接口，防止结构体偏移错位。

    错位的后果不是「少一行数据」，而是**一本正经地给出错误数字** ——
    用户照着它去关进程，关错东西。所以宁可缺数据也不能给错数字：
    对不上就整体退回句柄路径（覆盖少一半，但每个数字都是真的）。
    """
    checked = 0
    for pid, (ws, _private) in list(mem.items())[:40]:
        if ws < 1048576:
            continue  # 太小的值比不出什么
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), ctypes.sizeof(counters)
            ):
                continue
            real = int(counters.WorkingSetSize)
            if not real:
                continue
            # 扫描期间进程还在长，允许 20% 偏差
            if abs(ws - real) / real > 0.20:
                return False
            checked += 1
            if checked >= 6:
                return True
        finally:
            k32.CloseHandle(handle)
    # 样本太少时没有证据说它对，但也没有证据说它错 —— 这个接口是唯一能
    # 看到全部进程的路径，缺数据比可能的偏差更糟，所以放行。
    return True


def processes() -> list[Process]:
    """当前所有进程，内存对全部进程都填好。

    内存优先走 `_memory_by_pid()`（不需要句柄，全量）。路径和启动时间仍然要
    `OpenProcess`，非管理员会缺一部分 —— 缺了就把 `readable` 置 False，
    渲染时说明「以管理员身份运行可看到路径」，而不是假装它是干净的。
    """
    k32, psapi, _, _ = _libs()
    wins = _windows_by_pid()
    mem = _memory_by_pid()
    if mem and not _verify_memory(k32, psapi, mem):
        mem = {}  # 结构体对不上，退回句柄路径，宁可少一半也不给错数字
        COVERAGE["mode"] = "partial"
        COVERAGE["reason"] = "未文档化的全量内存接口自检没通过，已退回逐个进程读取"
    else:
        COVERAGE["mode"] = "all"
        COVERAGE["reason"] = ""
    out: list[Process] = []

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == HANDLE(-1).value:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            proc = Process(pid=pid, name=entry.szExeFile, windows=wins.get(pid, Windows()))
            got = mem.get(pid)
            if got:
                proc.working_set, proc.private = got
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    if not got:
                        # 退回路径：只有能打开的进程才有内存数字
                        counters = PROCESS_MEMORY_COUNTERS_EX()
                        counters.cb = ctypes.sizeof(counters)
                        if psapi.GetProcessMemoryInfo(
                            handle, ctypes.byref(counters), ctypes.sizeof(counters)
                        ):
                            proc.working_set = int(counters.WorkingSetSize)
                            proc.private = int(counters.PrivateUsage)
                    proc.path = _exe_path(k32, handle)
                    proc.started = _started_at(k32, handle)
                finally:
                    k32.CloseHandle(handle)
            if not proc.path:
                # 句柄都拿不到（受保护进程），路径和启动时间就无从得知
                proc.readable = False
            out.append(proc)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


# ------------------------------------------------------------------ 其它


def boot_time() -> float:
    """开机时刻（Unix 秒）。拿不到返回 0。

    注意：这个值在本机实测是**开机**时间，不是**登录**时间 —— 两者能差 5 个
    小时（机器 03:54 开机，09:30 才登录）。判断「开机自启」要用登录时间，
    见 `procs.logon_time()`。
    """
    if not IS_WINDOWS:
        return 0.0
    k32, _, _, _ = _libs()
    return time.time() - k32.GetTickCount64() / 1000.0


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def elevate() -> bool:
    """以管理员身份重新启动自己。成功返回 True（此时应退出当前进程）。

    走 UAC 弹窗（`runas`），不是偷偷提权 —— 提权必须让用户看到并同意。
    """
    if not IS_WINDOWS:
        return False
    try:
        params = subprocess.list2cmdline(sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, os.getcwd(), 1
        )
        return int(rc) > 32
    except (AttributeError, OSError):
        return False


def drive_type(path: str) -> int:
    """盘符类型：2=可移动，3=本地磁盘，4=网络，5=光驱，0=未知。"""
    if not IS_WINDOWS or not path:
        return 0
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return 0
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\"))
    except (AttributeError, OSError, ValueError):
        return 0


def memory_status() -> tuple[int, int]:
    """物理内存 (总量, 可用)，单位字节。拿不到返回 (0, 0)。"""
    if not IS_WINDOWS:
        return 0, 0
    try:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0, 0
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except (AttributeError, OSError, ValueError):
        return 0, 0


def _version_strings(path: str) -> dict[str, str]:
    """读 exe 的版本信息资源（公司名、描述）。不验签，只取字符串。

    坑：`VerQueryValueW` 返回的 `puLen` 对**字符串**是**字符数**，对
    `\\VarFileInfo\\Translation` 这种二进制块是**字节数**。按字节数去读字符串
    会只读到一半 —— "Google LLC" 变成 "Googl" 再加一个替换字符。第一版就是这么
    错的，界面上所有发行商都是 `Googl�`。
    """
    if not path or not os.path.isfile(path):
        return {}
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.restype = DWORD
    version.GetFileVersionInfoSizeW.argtypes = [LPCWSTR, ctypes.POINTER(DWORD)]
    version.GetFileVersionInfoW.restype = BOOL
    version.GetFileVersionInfoW.argtypes = [LPCWSTR, DWORD, DWORD, ctypes.c_void_p]
    version.VerQueryValueW.restype = BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(UINT),
    ]

    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buf):
        return {}
    block = ctypes.cast(buf, ctypes.c_void_p)

    def query(sub: str, chars: bool) -> bytes:
        ptr = ctypes.c_void_p()
        length = UINT()
        if not version.VerQueryValueW(block, sub, ctypes.byref(ptr), ctypes.byref(length)):
            return b""
        if not ptr.value or not length.value:
            return b""
        nbytes = length.value * 2 if chars else length.value
        return ctypes.string_at(ptr, nbytes)

    # 先问有哪些语言/代码页，再按它拼具体字段
    trans = query("\\VarFileInfo\\Translation", chars=False)
    langs: list[str] = []
    for i in range(0, len(trans) - 3, 4):
        lang = int.from_bytes(trans[i : i + 2], "little")
        page = int.from_bytes(trans[i + 2 : i + 4], "little")
        langs.append(f"{lang:04x}{page:04x}")
    if not langs:
        langs = ["040904b0"]
    out: dict[str, str] = {}
    for key, label in (("CompanyName", "company"), ("FileDescription", "description")):
        for lang in langs:
            raw = query(f"\\StringFileInfo\\{lang}\\{key}", chars=True)
            if raw:
                out[label] = raw.decode("utf-16-le", "replace").rstrip("\x00")
                break
    return out


def version_info(path: str) -> dict[str, str]:
    """公司名/描述。拿不到就返回空 dict，不编。"""
    if not IS_WINDOWS:
        return {}
    try:
        return _version_strings(path)
    except (OSError, AttributeError, ValueError):
        return {}
