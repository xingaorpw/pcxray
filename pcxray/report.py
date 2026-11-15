"""把自启项、进程、内存合成一份报告，并渲染成人能读的文字。

两条诚实纪律贯穿这个文件：

1. **读不到就说读不到。** 非管理员看不到一部分进程的路径，PowerShell 挂了
   读不到计划任务 —— 这些都要显示成「读不到」，不能变成空白。
2. **不重复计算。** 两个 Edge 自启项指向同一个 msedge.exe，按条目加内存会
   算两遍。所以「自启占了多少内存」按 **PID 去重** 求和。
"""

from __future__ import annotations

import os
import time

from . import autostart, procs, safety, text, winapi

__all__ = ["Item", "Report", "build", "render"]


class Item:
    """一条自启项 + 关于它的所有判断结果。"""

    __slots__ = (
        "entry",
        "exists",
        "company",
        "description",
        "suspect",
        "hidden",
        "runs",
        "run_mb",
        "refuse",
        "needs_force",
        "auto_started",
        "shared",
    )

    def __init__(self, entry: autostart.Entry):
        self.entry = entry
        self.exists: bool | None = None
        self.company = ""
        self.description = ""
        self.suspect = ""
        self.hidden = False
        self.runs: list[winapi.Process] = []
        self.run_mb = 0.0
        self.refuse = ""
        self.needs_force = False
        self.auto_started = False
        # 有多少个条目同时认领了同一批进程。两个条目指向同一个 exe 时
        # （本机就有两条 Edge 自启项），那 500 MB 会在两行里各出现一次。
        # 头部合计按 PID 去重，所以「行的和」会比「合计」大 —— 必须让用户
        # 看得见原因，否则会以为工具算错了。
        self.shared = 1

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def flags(self) -> list[str]:
        """短标记，给表格用。"""
        out = []
        if self.exists is False:
            out.append("文件不存在")
        if self.suspect:
            out.append("可疑路径")
        if self.hidden:
            out.append("不显示窗口")
        if self.runs:
            out.append("正在运行")
        if not self.company and self.exists:
            out.append("无发行商")
        if self.shared > 1:
            out.append(f"同一程序 {self.shared} 条")
        if self.needs_force and not self.refuse:
            out.append("需确认")
        # 扫描器发现但没法用一个短标签概括的情况（快捷方式解不出来、服务读不到
        # 可执行文件路径）都写在 entry.note 里。以前这些只进了 JSON，命令行表格
        # 一个字都不显示 —— 用户看到光秃秃一个文件名，以为一切正常。
        if self.entry.caveat:
            out.append(self.entry.caveat)
        return out

    def as_dict(self) -> dict:
        data = self.entry.as_dict()
        data.update(
            {
                "exists": self.exists,
                "company": self.company,
                "description": self.description,
                "suspect": self.suspect,
                "hidden_args": self.hidden,
                "running": [{"pid": p.pid, "mb": round(p.mb, 1)} for p in self.runs],
                "running_mb": round(self.run_mb, 1),
                "auto_started": self.auto_started,
                "shared": self.shared,
                "can_disable": not self.refuse,
                "needs_force": self.needs_force,
                "refuse": self.refuse,
                "flags": self.flags,
            }
        )
        return data


class Report:
    def __init__(self) -> None:
        self.items: list[Item] = []
        self.problems: list[str] = []
        self.groups: list[procs.Group] = []
        self.background: list[procs.Group] = []
        self.admin = False
        self.logon = 0.0
        self.boot = 0.0
        self.total_ram = 0
        self.avail_ram = 0
        self.memory_coverage = "all"
        self.process_count = 0
        self.unreadable = 0
        self.auto_mb = 0.0
        self.auto_count = 0

    # -------------------------------------------------- 汇总数字

    @property
    def enabled(self) -> list[Item]:
        return [i for i in self.items if i.entry.enabled]

    @property
    def disabled(self) -> list[Item]:
        return [i for i in self.items if not i.entry.enabled]

    def autostart_memory(self, items: list[Item] | None = None) -> float:
        """自启项现在实际占的内存（按 PID 去重）。

        `items` 不传就用全部条目；过滤视图下要传过滤后的，否则会出现
        「只筛出 3 条，却报全量内存」这种自相矛盾的数字。
        """
        pids: dict[int, float] = {}
        for item in self.items if items is None else items:
            if not item.entry.enabled:
                continue
            for proc in item.runs:
                pids[proc.pid] = proc.mb
        return sum(pids.values())

    def as_dict(self, only: str = "", limit: int = 0) -> dict:
        """转成 JSON。

        `only` / `limit` 必须真的生效 —— 第一版 `--json` 直接忽略了这两个参数，
        于是 `--json --kind task` 把 183 条全都吐出来，脚本按 kind 过滤时拿到
        的是错数据。计数也要跟着重算，不能让 total 和 items 对不上。
        """
        items = self.items
        if only:
            items = [i for i in items if i.entry.kind == only]
        # 计数说的是「这次查询命中多少」，items 只是其中显示出来的那一段。
        # 以前 limit 之后才统计，于是 `--json --limit 2` 会报 total=2 —— 脚本
        # 看到的是一个「只有两条自启项」的世界，完全不知道还有 181 条没给。
        matched = items
        shown = items[:limit] if limit else items
        enabled = [i for i in matched if i.entry.enabled]
        return {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "admin": self.admin,
            "memory_coverage": self.memory_coverage,
            "logon": self.logon,
            "boot": self.boot,
            "ram": {
                "total": self.total_ram,
                "available": self.avail_ram,
            },
            "process_count": self.process_count,
            "unreadable_processes": self.unreadable,
            "autostart": {
                "total": len(matched),
                "enabled": len(enabled),
                "disabled": len(matched) - len(enabled),
                "memory_mb": round(self.autostart_memory(matched), 1),
                # len(items) + hidden_by_limit == total，脚本一眼能看出被截了
                "hidden_by_limit": len(matched) - len(shown),
                "items": [i.as_dict() for i in shown],
            },
            "background": [
                {
                    "name": g.name,
                    "count": len(g.pids),
                    "mb": round(g.mb, 1),
                    "path": g.paths[0] if g.paths else "",
                    "started": g.started,
                }
                for g in self.background
            ],
            "groups": [g.as_dict() for g in self.groups],
            "problems": self.problems,
        }


# ------------------------------------------------------------------ 构建


def _annotate(entry: autostart.Entry, procs_list: list[winapi.Process], admin: bool) -> Item:
    item = Item(entry)
    exe = entry.exe
    if exe:
        item.exists = os.path.isfile(exe)
        if item.exists:
            info = winapi.version_info(exe)
            item.company = info.get("company", "")
            item.description = info.get("description", "")
        item.suspect = safety.suspect_path(exe)
    else:
        item.exists = None  # 连路径都没解析出来，不知道存不存在
    item.hidden = safety.hidden_args(entry.command)
    item.runs = procs.running_of(exe, procs_list)
    item.run_mb = sum(p.mb for p in item.runs)
    item.refuse = safety.refuse_reason(
        entry.kind, entry.scope, entry.name, entry.protected, admin
    )
    item.needs_force = safety.needs_force(entry.kind) and not item.refuse
    return item


def build(
    include: tuple[str, ...] = ("registry", "folders", "tasks", "services"),
    entries: list[autostart.Entry] | None = None,
    process_list: list[winapi.Process] | None = None,
) -> Report:
    report = Report()
    report.admin = winapi.is_admin()
    process_list = process_list if process_list is not None else winapi.processes()
    if entries is None:
        entries, problems = autostart.scan(include)
        report.problems = list(problems)
    report.process_count = len(process_list)
    report.unreadable = sum(1 for p in process_list if not p.readable)
    report.memory_coverage = winapi.COVERAGE["mode"]
    if report.memory_coverage != "all":
        report.problems.append(winapi.COVERAGE["reason"])
    report.logon = procs.logon_time(process_list)
    report.boot = winapi.boot_time()
    report.total_ram, report.avail_ram = winapi.memory_status()
    report.groups = procs.group(process_list)
    # 后台那一块按程序合并：同一个程序开 6 个进程时，用户要看的是「它一共占
    # 多少」，不是 6 行同名条目（第一版就是这样，看起来像重复 bug）。
    report.background = procs.group(procs.background_heavy(process_list))
    report.items = [_annotate(e, process_list, report.admin) for e in entries]
    # 同一批进程可能被多个条目认领（两条 Edge 自启项都指向 msedge.exe）。
    # 头部合计按 PID 去重，行里的数字是「这个程序一共占多少」，所以行的和
    # 会比合计大。把这件事标在行上，用户才知道该信哪个数。
    owners: dict[int, int] = {}
    for item in report.items:
        for proc in item.runs:
            owners[proc.pid] = owners.get(proc.pid, 0) + 1
    for item in report.items:
        item.shared = max((owners[p.pid] for p in item.runs), default=1)
    auto_pids = procs.autostart_pids(process_list, report.logon)
    report.auto_count = len(auto_pids)
    report.auto_mb = sum(p.mb for p in process_list if p.pid in auto_pids)
    for item in report.items:
        item.auto_started = any(p.pid in auto_pids for p in item.runs)
    return report


# ------------------------------------------------------------------ 渲染


def _state(entry: autostart.Entry) -> str:
    return "启用" if entry.enabled else "已禁用"


def _kind_label(kind: str) -> str:
    return {
        "run": "注册表 Run",
        "run32": "注册表 Run（32 位）",
        "runonce": "注册表 RunOnce",
        "folder": "启动文件夹",
        "task": "计划任务",
        "service": "自动服务",
    }.get(kind, kind)


def _short_source(entry: autostart.Entry) -> str:
    """来源的短写法。长名字会把整张表撑歪。"""
    if entry.kind == "folder":
        return "用户启动夹" if entry.scope == "user" else "公共启动夹"
    if entry.kind == "task":
        return "计划任务"
    if entry.kind == "service":
        return "服务"
    if entry.kind == "run32":
        return "所有用户 Run32"
    if entry.kind == "runonce":
        return "RunOnce"
    return "用户 Run" if entry.scope == "user" else "所有用户 Run"


def render(report: Report, limit: int = 0, only: str = "") -> list[str]:
    """渲染成终端文本。

    默认只把「注册表 Run + 启动文件夹」铺开 —— 那是用户真正会去关的东西。
    计划和任务/服务加起来一百多条，全铺出来会把有用的信息挤出屏幕；它们的
    数量单独给一行，要看用 `--kind`。
    """
    lines: list[str] = []
    lines.append("")
    lines.append(f"  pcxray · {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # ---- 概览
    if report.total_ram:
        used = report.total_ram - report.avail_ram
        lines.append(
            f"  内存 {text.mb(report.total_ram / 1048576)}  "
            f"已用 {text.mb(used / 1048576)}  "
            f"可用 {text.mb(report.avail_ram / 1048576)}"
        )
    if report.boot:
        boot_text = time.strftime("%m-%d %H:%M", time.localtime(report.boot))
        if report.logon:
            lines.append(
                f"  开机 {boot_text}"
                f"　登录 {time.strftime('%m-%d %H:%M', time.localtime(report.logon))}"
            )
        else:
            # 读不到 explorer、也没有可读的进程启动时间。这时候写「登录 03:54」
            # 就是把开机时间冒充成登录时间 —— 宁可说不知道。
            lines.append(f"  开机 {boot_text}　登录时间未知")
    # 有来源读不到时，「自启 0 条」是假的 —— 这台机器可能有 183 条，只是没读到。
    # 数字旁边必须带一句，否则用户会以为系统很干净（CONTRIBUTING 第 5 条）。
    missed = f"，另有 {len(report.problems)} 部分没读到（见下方）" if report.problems else ""
    lines.append(
        f"  进程 {report.process_count} 个"
        f"　自启 {len(report.items)} 条（启用 {len(report.enabled)}）{missed}"
    )
    auto_mb = report.autostart_memory()
    if auto_mb:
        # 「行的和」会比这个数大：同一个程序被两条自启项指向时，两行都会显示
        # 它的全部内存。所以这里说清楚这是去重后的合计。
        lines.append(
            f"  自启项现在占着 {text.mb(auto_mb)}"
            f"（同一个进程只算一次；行里的数字是各自程序的全部内存）"
        )
        if report.auto_count:
            lines.append(
                f"  登录后 90 秒内起来的 {report.auto_count} 个进程共 {text.mb(report.auto_mb)}"
            )
    lines.append("")

    # ---- 自启表
    quick = ("run", "run32", "folder", "runonce")
    if only:
        shown = [i for i in report.items if i.entry.kind == only or i.entry.source.startswith(only)]
        rest: list[Item] = []
    else:
        shown = [i for i in report.items if i.entry.kind in quick]
        rest = [i for i in report.items if i.entry.kind not in quick]

    hidden_by_limit = 0
    if limit and len(shown) > limit:
        # 截断了就必须说，否则用户看到 3 行会以为「这一类就 3 条」
        hidden_by_limit = len(shown) - limit
        shown = shown[:limit]
    if shown:
        rows = []
        for item in shown:
            mark = []
            if item.exists is False:
                mark.append("!文件没了")
            if item.suspect:
                mark.append("!可疑")
            if item.hidden:
                mark.append("不显窗")
            if item.runs:
                mark.append(f"占{text.mb(item.run_mb)}")
            if item.shared > 1:
                mark.append(f"×{item.shared}条")
            # 扫描器的话（快捷方式解不出来、服务读不到路径）也得进表格，
            # 不然命令行用户只看到一个光秃秃的文件名
            if item.entry.caveat:
                mark.append(item.entry.caveat)
            rows.append(
                [
                    _state(item.entry),
                    item.name,
                    _short_source(item.entry),
                    item.company or "—",
                    " ".join(mark),
                ]
            )
        if only:
            header = f"  {_kind_label(only)}"
        else:
            # 只扫了一部分来源时别把没扫的也写进标题（--include registry 就只
            # 扫了注册表，标题写「注册表 + 启动文件夹」是错的）
            kinds = {i.entry.kind for i in report.items}
            if kinds and kinds <= {"run", "run32", "runonce"}:
                header = "  开机自启（注册表）"
            elif kinds == {"folder"}:
                header = "  开机自启（启动文件夹）"
            else:
                header = "  开机自启（注册表 + 启动文件夹）"
        lines.append(header)
        lines.extend(
            "  " + line
            for line in text.table(
                ["状态", "名称", "来源", "发行商", "备注"],
                rows,
                ["left", "left", "left", "left", "left"],
                limits=[6, 46, 14, 18, 28],
            )
        )
        if hidden_by_limit:
            lines.append(
                f"  · 只显示了前 {len(shown)} 条，还有 {hidden_by_limit} 条没显示"
                f"（--limit 0 看全部）"
            )
        lines.append("")

    # ---- 其余来源只给计数
    if rest:
        counts: dict[str, list[Item]] = {}
        for item in rest:
            counts.setdefault(item.entry.kind, []).append(item)
        parts = []
        for kind in ("task", "service"):
            group = counts.get(kind)
            if not group:
                continue
            on = sum(1 for i in group if i.entry.enabled)
            parts.append(f"{_kind_label(kind)} {len(group)} 条（启用 {on}）")
        if parts:
            lines.append("  还有：" + "　".join(parts))
            lines.append("  　用 pcxray list --kind task / --kind service 看明细")
            lines.append("")

    # ---- 后台
    if report.background:
        lines.append("  后台占内存（没有可见窗口，超过 100 MB）")
        rows = []
        for group in report.background[:15]:
            rows.append(
                [
                    group.name,
                    str(len(group.pids)),
                    text.mb(group.mb),
                    procs.describe_started(group.started, report.logon),
                    group.paths[0] if group.paths else "读不到路径",
                ]
            )
        lines.extend(
            "  " + line
            for line in text.table(
                ["程序", "进程", "内存", "启动", "路径"],
                rows,
                ["left", "right", "right", "left", "left"],
                limits=[26, 5, 9, 18, 40],
            )
        )

    # ---- 提示
    notes: list[str] = []
    if not report.admin and report.unreadable:
        # 一个都读不到时（比如空机器）这句话没有信息量，只会让人以为自己漏看了什么
        notes.append(
            f"不是管理员：{report.unreadable} 个进程读不到路径和启动时间。"
            "要完整信息用 pcxray list --elevate"
        )
    if report.memory_coverage != "all":
        notes.append("内存数据只覆盖了一部分进程（见上面的 problems）")
    for problem in report.problems:
        # problems 里可能有 PowerShell 的原始报错（带 CRLF）和任务名/服务名。
        # 直接拼进提示行，`\x1b[2J` 就能清屏、`\r` 能把行冲掉 —— 这里也要过 safe。
        flat = problem.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        notes.append(text.safe(flat))
    if notes:
        lines.append("")
        for note in notes:
            lines.append(f"  · {note}")
    lines.append("")
    # 没有任何表格区块时，上面会留下相邻的空行 —— 压成一个
    collapsed: list[str] = []
    for line in lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    return collapsed
