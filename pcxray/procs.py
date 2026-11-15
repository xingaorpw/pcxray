"""把进程列表变成能回答问题的形状。

要回答的两个问题：

1. **谁在后台占内存？** 按程序名合并（Chrome 开 20 个进程，用户想知道的是
   「Chrome 占 3 GB」，不是 20 行各 150 MB）。
2. **哪些是开机自启拉起来的？** 两条证据：一是和自启项的可执行文件路径**精确**
   对上；二是启动时间紧挨着登录时间。两条都记下来，分开标注，不混为一谈。

关于「登录时间」：本机实测开机 03:54、登录 09:30，差了 5 个半小时。拿开机时间
当基准会把所有登录后才启动的程序都算成「不是自启」。所以基准取 explorer.exe
（shell 一起来就说明登录了）的启动时间。
"""

from __future__ import annotations

import time

from . import safety, winapi

__all__ = [
    "Group",
    "logon_time",
    "group",
    "running_of",
    "background_heavy",
    "autostart_pids",
    "LOGON_WINDOW",
]

# 登录后多久内启动算「自启拉起」。90 秒够覆盖慢启动的机械硬盘和杀软扫描，
# 又不会把用户自己后来点开的程序算进来。
LOGON_WINDOW = 90.0


class Group:
    """同名进程合并后的一行。"""

    __slots__ = ("name", "pids", "mb", "private_mb", "paths", "has_window", "started", "readable", "company")

    def __init__(self, name: str):
        self.name = name
        self.pids: list[int] = []
        self.mb = 0.0
        self.private_mb = 0.0
        self.paths: list[str] = []
        self.has_window = False
        self.started = 0.0  # 组内最早启动时间
        self.readable = False
        self.company = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "count": len(self.pids),
            "mb": round(self.mb, 1),
            "private_mb": round(self.private_mb, 1),
            "path": self.paths[0] if self.paths else "",
            "has_window": self.has_window,
            "started": self.started,
            "company": self.company,
        }


def logon_time(procs: list[winapi.Process]) -> float:
    """登录时刻的估计值。

    优先用 explorer.exe 的启动时间（shell 起来 = 用户登录完成）。
    拿不到就退回所有可读进程里最早的那个；再拿不到返回 0（= 不知道）。
    """
    for proc in procs:
        if proc.name.lower() == "explorer.exe" and proc.started:
            return proc.started
    candidates = [p.started for p in procs if p.started and p.readable]
    if candidates:
        return min(candidates)
    # 什么都不知道就返回 0，不要拿开机时间冒充登录时间 —— 那会让界面打出
    # 「开机 03:54　登录 03:54」这种看起来精确、其实是编的数字。
    return 0.0


def group(procs: list[winapi.Process]) -> list[Group]:
    """按程序名合并，按内存从大到小排。"""
    table: dict[str, Group] = {}
    for proc in procs:
        key = proc.name.lower()
        item = table.get(key)
        if item is None:
            item = table[key] = Group(proc.name)
        item.pids.append(proc.pid)
        item.mb += proc.mb
        item.private_mb += proc.private_mb
        if proc.path and proc.path not in item.paths:
            item.paths.append(proc.path)
        if not proc.windows.hidden:
            item.has_window = True
        if proc.started and (not item.started or proc.started < item.started):
            item.started = proc.started
        if proc.readable:
            item.readable = True
    out = sorted(table.values(), key=lambda g: -g.mb)
    return out


# 这些可执行文件是「宿主」，几十个不同的东西共用同一个 exe。
# 按路径把进程算到某一条自启项头上会得到荒唐的数字（比如 114 个服务里凡是
# 用 svchost 的都显示占 400 MB），所以这几类干脆不做归属，只显示名字。
_SHARED_HOSTS = frozenset(
    {
        "svchost.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "wscript.exe",
        "cscript.exe",
        "rundll32.exe",
        "mshta.exe",
        "conhost.exe",
        "dllhost.exe",
        "taskhostw.exe",
    }
)


def _win_base(path: str) -> str:
    """取 Windows 路径的文件名。

    不能用 `os.path.basename` —— 在 Linux 上它不认 `\\` 当分隔符，会把
    `C:\\Windows\\system32\\cmd.exe` 整串当成文件名。这个模块的输入永远是
    Windows 路径，所以自己按两种分隔符切。
    """
    cleaned = (path or "").strip().strip('"').rstrip("\\/")
    for sep in ("\\", "/"):
        if sep in cleaned:
            cleaned = cleaned.rsplit(sep, 1)[-1]
    return cleaned.lower()


def _win_key(path: str) -> str:
    """Windows 路径的比较键：统一分隔符、去掉引号、转小写。

    同样不能用 `os.path.normcase` —— 它在 Linux 上是空操作，大小写不同的
    同一条路径会比不相等，测试在 Ubuntu 上就是这么挂的。
    """
    cleaned = (path or "").strip().strip('"')
    cleaned = cleaned.replace("/", "\\").rstrip("\\")
    return cleaned.lower()


def running_of(exe: str, procs: list[winapi.Process]) -> list[winapi.Process]:
    """路径精确匹配正在运行的进程。

    只比路径，不比命令行参数 —— 参数会变（比如 `--wake` 只在自动唤醒时出现），
    按参数比会让「这个自启项现在占多少内存」时有时无。
    """
    if not exe:
        return []
    if _win_base(exe) in _SHARED_HOSTS:
        return []  # 宿主程序，归属没有意义
    want = _win_key(exe)
    return [p for p in procs if p.path and _win_key(p.path) == want]


def autostart_pids(procs: list[winapi.Process], logon: float) -> set[int]:
    """登录后 90 秒内启动的进程 —— 这些几乎肯定是被自启项拉起来的。"""
    if not logon:
        return set()
    return {p.pid for p in procs if p.started and 0 <= p.started - logon <= LOGON_WINDOW}


def background_heavy(procs: list[winapi.Process], min_mb: float = 100.0) -> list[winapi.Process]:
    """没有可见窗口、又占了不少内存的进程。

    排除系统进程：svchost、Memory Compression 这些本来就不该有窗口，
    把它们列进「偷偷占内存」只会制造噪音。
    """
    out = [
        p
        for p in procs
        if p.windows.hidden
        and p.mb >= min_mb
        and not safety.protected_process(p.name)
        and not p.name.lower().startswith("svchost")
    ]
    out.sort(key=lambda p: -p.mb)
    return out


def describe_started(when: float, logon: float) -> str:
    """把启动时间说成人话。"""
    if not when:
        return "启动时间未知"
    clock = time.strftime("%H:%M", time.localtime(when))
    if logon and 0 <= when - logon <= LOGON_WINDOW:
        return f"{clock} 随登录启动"
    if logon and when < logon:
        return f"{clock} 登录前启动"
    return f"{clock} 启动"
