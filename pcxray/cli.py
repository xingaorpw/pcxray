"""命令行入口。

设计取向：**默认只读**。`pcxray` 不带参数就是列一遍自启，什么也不改。
要改必须点名到具体条目，而且匹配不唯一时直接拒绝 —— 「差不多就关这个吧」
是这个工具最不该有的行为。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, actions, autostart, procs, report as report_mod, text, winapi

__all__ = ["main", "resolve"]


def _setup_console() -> None:
    """让中文能正常打到控制台。

    Windows 的控制台代码页可能是 cp1252（英文系统）或 cp936（中文系统），
    直接 `print` 中文会抛 `UnicodeEncodeError` 把整个命令打断 —— CI 上就
    是这样挂的。两步：先把控制台代码页切到 UTF-8，再把 Python 的 stdout
    换成 UTF-8 且 `errors="replace"`。

    切不动也不要紧：`errors="replace"` 保证最坏情况是显示成问号，而不是崩溃。
    """
    if winapi.IS_WINDOWS:
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except (OSError, AttributeError, ImportError):
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def resolve(entries: list[autostart.Entry], query: str) -> tuple[autostart.Entry | None, list[autostart.Entry]]:
    """把用户写的名字解析成唯一一条。返回 (命中, 候选列表)。

    三级匹配：稳定 id → 完整名字（不分大小写）→ 名字包含。
    最后一级命中多条时返回候选，由调用方拒绝执行。
    """
    query = (query or "").strip()
    if not query:
        return None, []
    for entry in entries:
        if entry.key == query:
            return entry, []
    lowered = query.lower()
    exact = [e for e in entries if e.name.lower() == lowered]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    partial = [e for e in entries if lowered in e.name.lower()]
    if len(partial) == 1:
        return partial[0], []
    return None, partial


def _print_results(results: list[actions.Result]) -> int:
    """一行一条结果。不用表格 —— 拒绝理由可能很长（比如「匹配到 2 条，请写更
    完整的名字：A、B」），表格会把整行撑到屏幕外面去。"""
    ok = sum(1 for r in results if r.ok)
    print("")
    for result in results:
        mark = "✓" if result.ok else "✗"
        head = f"  {mark} {result.name}"
        message = result.message
        if not message:
            print(head)
            continue
        # 名字短的时候把结果接在同一行，长的时候换行，读起来更顺
        if text.width(head) + 2 + text.width(message) <= 96:
            print(f"{head}　{message}")
        else:
            print(head)
            for line in text.wrap(message, 88, indent="      "):
                print(line)
    print("")
    return 0 if ok == len(results) else 1


def _load_entries(args) -> tuple[list[autostart.Entry], list[str]]:
    include = tuple(args.include.split(",")) if args.include else ("registry", "folders", "tasks", "services")
    return autostart.scan(include)


def _cmd_list(args) -> int:
    entries, problems = _load_entries(args)
    process_list = winapi.processes()
    if args.json:
        rep = report_mod.build(entries=entries, process_list=process_list)
        rep.problems.extend(problems)
        print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2))
        return 0
    rep = report_mod.build(entries=entries, process_list=process_list)
    rep.problems.extend(p for p in problems if p not in rep.problems)
    for line in report_mod.render(rep, limit=args.limit, only=args.kind or ""):
        print(line)
    return 0


def _cmd_ram(args) -> int:
    process_list = winapi.processes()
    logon = procs.logon_time(process_list)
    groups = procs.group(process_list)
    if args.json:
        print(
            json.dumps(
                {
                    "logon": logon,
                    "coverage": winapi.COVERAGE,
                    "total_mb": round(sum(g.mb for g in groups), 1),
                    "groups": [g.as_dict() for g in groups],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("")
    total, avail = winapi.memory_status()
    if total:
        print(
            f"  内存 {text.mb(total / 1048576)}　"
            f"可用 {text.mb(avail / 1048576)}　"
            f"进程 {len(process_list)} 个"
        )
        print("")
    rows = []
    for item in groups[: args.limit or 25]:
        rows.append(
            [
                item.name,
                str(len(item.pids)),
                text.mb(item.mb),
                text.mb(item.private_mb),
                "有" if item.has_window else "无",
                procs.describe_started(item.started, logon),
            ]
        )
    for line in text.table(
        ["程序", "进程数", "占用", "提交", "窗口", "启动"],
        rows,
        ["left", "right", "right", "right", "left", "left"],
        limits=[28, 8, 10, 10, 6, 20],
    ):
        print("  " + line)
    print("")
    print("  「占用」= 工作集，和任务管理器那一列一致")
    print("  「提交」= 已申请的内存，含被换到页面文件的部分，可能比占用大好几倍")
    if winapi.COVERAGE["mode"] != "all":
        print(f"  · {winapi.COVERAGE['reason']}")
    if not winapi.is_admin():
        print("  · 不是管理员：部分系统进程的路径和启动时间读不到，内存数字不受影响")
    print("")
    return 0


def _cmd_off(args) -> int:
    entries, _problems = _load_entries(args)
    picked, results = _pick(entries, args.names)
    if not picked:
        return _print_results(results)
    done, backup = actions.disable(
        picked, force=args.force, dry_run=args.dry_run, mode=args.mode
    )
    code = _print_results(done)
    if backup:
        print("")
        print(f"  备份已写入：{backup}")
        print(f"  反悔就运行：pcxray restore \"{backup}\"")
    if not args.dry_run and code == 0:
        print("")
        print("  重启后生效。想立刻确认这一条还会不会启动，用 --mode move 再关一次。")
    return code


def _cmd_on(args) -> int:
    entries, _problems = _load_entries(args)
    picked, results = _pick(entries, args.names)
    if not picked:
        return _print_results(results)
    done, backup = actions.enable(
        picked, force=args.force, dry_run=args.dry_run, mode=args.mode
    )
    code = _print_results(done)
    if backup:
        print("")
        print(f"  备份已写入：{backup}")
    return code


def _pick(entries, names: list[str]) -> tuple[list[autostart.Entry], list[actions.Result]]:
    picked: list[autostart.Entry] = []
    problems: list[actions.Result] = []
    for name in names:
        hit, candidates = resolve(entries, name)
        if hit is not None:
            picked.append(hit)
            continue
        if candidates:
            names_shown = "、".join(f"{c.name}（{c.source}）" for c in candidates[:6])
            problems.append(
                actions.Result(name, False, f"匹配到 {len(candidates)} 条，请写更完整的名字：{names_shown}")
            )
        else:
            problems.append(actions.Result(name, False, "没有这一条，先跑 pcxray list 看看名字"))
    return picked, problems


def _cmd_restore(args) -> int:
    done, path = actions.restore(args.backup or "", dry_run=args.dry_run)
    code = _print_results(done)
    if path:
        print("")
        print(f"  使用备份：{path}")
    return code


def _cmd_backups(args) -> int:
    backups = actions.list_backups()
    if not backups:
        print("  还没有任何备份（说明你还没改过东西）")
        return 0
    for path in backups:
        print(f"  {path}")
    return 0


def _cmd_web(args) -> int:
    from . import web

    return web.serve(port=args.port, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcxray",
        description="看清开机自启和后台占内存，需要时再关掉。默认只读。",
    )
    parser.add_argument("--version", action="version", version=f"pcxray {__version__}")
    sub = parser.add_subparsers(dest="command")

    def common(p):
        p.add_argument("--json", action="store_true", help="输出 JSON")
        p.add_argument("--limit", type=int, default=0, help="最多显示多少行")
        p.add_argument("--kind", default="", help="只看某一类：run/folder/task/service/runonce")
        p.add_argument(
            "--include",
            default="",
            help="只扫这些来源（逗号分隔）：registry,folders,tasks,services",
        )
        p.add_argument("--elevate", action="store_true", help="以管理员身份重开（能看到更多进程信息）")

    p_list = sub.add_parser("list", help="列出自启项（默认命令）")
    common(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_ram = sub.add_parser("ram", help="看谁在后台占内存")
    common(p_ram)
    p_ram.set_defaults(func=_cmd_ram)

    p_off = sub.add_parser("off", help="关掉自启（会先备份）")
    p_off.add_argument("names", nargs="+", help="自启项名字或 id，可以写多个")
    p_off.add_argument("--dry-run", action="store_true", help="只说要做什么，不改")
    p_off.add_argument("--force", action="store_true", help="允许改服务（服务默认只给建议）")
    p_off.add_argument(
        "--mode",
        choices=[actions.MODE_FLAG, actions.MODE_MOVE],
        default=actions.MODE_FLAG,
        help="flag=改任务管理器那个开关（默认）；move=把注册表值改名，当场可见",
    )
    p_off.add_argument("--include", default="", help="只在这些来源里找")
    p_off.set_defaults(func=_cmd_off)

    p_on = sub.add_parser("on", help="恢复自启")
    p_on.add_argument("names", nargs="+", help="自启项名字或 id")
    p_on.add_argument("--dry-run", action="store_true")
    p_on.add_argument("--force", action="store_true")
    p_on.add_argument("--mode", choices=[actions.MODE_FLAG, actions.MODE_MOVE], default=actions.MODE_FLAG)
    p_on.add_argument("--include", default="")
    p_on.set_defaults(func=_cmd_on)

    p_restore = sub.add_parser("restore", help="按备份还原")
    p_restore.add_argument("backup", nargs="?", default="", help="备份文件，省略则用最近一份")
    p_restore.add_argument("--dry-run", action="store_true")
    p_restore.set_defaults(func=_cmd_restore)

    p_backups = sub.add_parser("backups", help="列出备份")
    p_backups.set_defaults(func=_cmd_backups)

    p_web = sub.add_parser("web", help="打开本地界面")
    p_web.add_argument("--port", type=int, default=0, help="端口，默认自动挑一个空闲的")
    p_web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_web.set_defaults(func=_cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "elevate", False) and not winapi.is_admin():
        if winapi.elevate():
            print("  已以管理员身份重新打开，请在新的窗口里看结果。")
            return 0
        print("  提权被取消或失败，继续用普通权限运行（会少看到一部分进程信息）")
        print("")

    if not getattr(args, "func", None):
        # 不带参数 = list，这是最常用的一条
        args = parser.parse_args(["list"] + argv)

    if not winapi.IS_WINDOWS:
        print("pcxray 只能在 Windows 上运行。")
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  已中断。")
        return 130
