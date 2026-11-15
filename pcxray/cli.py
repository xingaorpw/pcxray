"""命令行入口。

设计取向：**默认只读**。`pcxray` 不带参数就是列一遍自启，什么也不改。
要改必须点名到具体条目，而且匹配不唯一时直接拒绝 —— 「差不多就关这个吧」
是这个工具最不该有的行为。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
    # 第四级：把空格、下划线、连字符、点都去掉再比一次。用户记名字的时候
    # 不会记得 `clash_verge_service` 里那个下划线，敲 `clash verge` 就该能找到。
    # 只在「去掉分隔符后完全相等」时命中，不做模糊，免得关错东西。
    def squash(value: str) -> str:
        return "".join(ch for ch in value.lower() if ch not in " _-.\t")

    squashed = squash(query)
    if squashed:
        loose = [e for e in entries if squash(e.name) == squashed]
        if len(loose) == 1:
            return loose[0], []
        if len(loose) > 1:
            return None, loose
    partial = [e for e in entries if lowered in e.name.lower()]
    if len(partial) == 1:
        return partial[0], []
    return None, partial


def _width_note(lines: list[str]) -> str:
    """表格比窗口宽时提醒一句。

    折行之后表头和数据会错位，看起来像排版坏了 —— 用户会以为是程序的问题。
    取不到窗口宽度就什么都不说（重定向到文件时本来就没有窗口）。
    """
    try:
        columns = shutil.get_terminal_size().columns
    except OSError:
        return ""
    if columns <= 0:
        return ""
    need = text.widest(lines)
    if need <= columns:
        return ""
    return f"  · 表格有 {need} 列宽，终端只有 {columns} 列，会折行；把窗口拉宽，或改用 --json"


def _print_results(results: list[actions.Result]) -> int:
    """一行一条结果。不用表格 —— 拒绝理由可能很长（比如「匹配到 2 条，请写更
    完整的名字：A、B」），表格会把整行撑到屏幕外面去。"""
    ok = sum(1 for r in results if r.ok)
    print("")
    for result in results:
        mark = "✓" if result.ok else "✗"
        # 名字可能来自注册表值名（里面能塞转义序列），理由里也带着名字
        head = f"  {mark} {text.safe(result.name)}"
        message = text.safe(result.message)
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


def _parse_include(value: str) -> list[str]:
    """把 `--include` 拆成来源名。

    逗号后面带空格是最自然的写法（`--include "registry, folders"`），以前会被
    当成「不认识的 --include： folders」，前面的空格还在提示里，看着像工具坏了。
    """
    out: list[str] = []
    for part in (value or "").split(","):
        part = part.strip().lower()
        if part and part not in out:
            out.append(part)
    return out


def _load_entries(args) -> tuple[list[autostart.Entry], list[str]]:
    include = tuple(_parse_include(args.include)) or ("registry", "folders", "tasks", "services")
    return autostart.scan(include)


_KNOWN_KINDS = ("run", "run32", "runonce", "folder", "task", "service")
_KNOWN_INCLUDE = ("registry", "folders", "tasks", "services")


def _cmd_list(args) -> int:
    if args.limit < 0:
        # 负数是坑：`shown[:-1]` 会静默少掉最后一行，看起来像「数据就这么点」
        print("  ✗ --limit 不能是负数。0 表示不限，其它值是最多显示几行。", file=sys.stderr)
        return 2
    # `--kind " task "` / `--kind TASK` 都该认：大小写和多余空格不是用户的错
    kind = (args.kind or "").strip().lower()
    if kind and kind not in _KNOWN_KINDS:
        # 不认识的 kind 以前会静默输出一份正常报告，用户会以为「这一类没有条目」
        print(
            f"  ✗ 不认识的 --kind：{text.safe(args.kind)}\n"
            f"    可选：{'、'.join(_KNOWN_KINDS)}",
            file=sys.stderr,
        )
        return 2
    if args.include:
        bad = [p for p in _parse_include(args.include) if p not in _KNOWN_INCLUDE]
        if bad:
            # 静默忽略会变成「扫了 0 条」，用户以为系统很干净
            print(
                f"  ✗ 不认识的 --include：{'、'.join(text.safe(b) for b in bad)}\n"
                f"    可选：{'、'.join(_KNOWN_INCLUDE)}",
                file=sys.stderr,
            )
            return 2
    entries, problems = _load_entries(args)
    process_list = winapi.processes()
    if args.json:
        rep = report_mod.build(entries=entries, process_list=process_list)
        rep.problems.extend(problems)
        print(
            json.dumps(
                rep.as_dict(only=kind, limit=args.limit),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    rep = report_mod.build(entries=entries, process_list=process_list)
    rep.problems.extend(p for p in problems if p not in rep.problems)
    lines = list(report_mod.render(rep, limit=args.limit, only=kind))
    for line in lines:
        print(line)
    note = _width_note(lines)
    if note:
        print(note)
    return 0


def _cmd_ram(args) -> int:
    if args.limit is not None and args.limit < 0:
        # `groups[:-5]` 会静默丢掉最后 5 组，看起来像「程序就这么几个」
        print("  ✗ --limit 不能是负数。0 表示不限，其它值是最多显示几行。", file=sys.stderr)
        return 2
    process_list = winapi.processes()
    logon = procs.logon_time(process_list)
    groups = procs.group(process_list)
    if args.json:
        # total_mb 必须等于「各组 mb 相加」—— 用户会拿计算器核。先各自取到
        # 一位小数再求和，否则四舍五入误差会累积出零点几 MB 的差。
        rounded = [round(g.mb, 1) for g in groups]
        # --limit 在 --json 下也要生效，而且要说明截了 —— 和 list 的语义对齐。
        # 不传 limit 时给全部（脚本通常要全量）。
        shown_json = groups[: args.limit] if args.limit else groups  # None / 0 都给全部
        print(
            json.dumps(
                {
                    "logon": logon,
                    "coverage": winapi.COVERAGE,
                    "total_mb": round(sum(rounded), 1),
                    "group_count": len(groups),
                    "hidden_by_limit": len(groups) - len(shown_json),
                    "groups": [g.as_dict() for g in shown_json],
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
    # --limit 的语义在两条命令里必须一致：0 = 全部。不写（None）= 表格默认 25 行，
    # 这个默认值只在**表格**这条路径上生效 —— 不能写回 argparse 的 default，否则
    # `ram --json` 会被表格的默认值截掉（脚本要的是全量）。
    # 这样 `ram --json` 能拿到全量（脚本要的就是全量），而表格不会一次刷 159 行。
    cap = 25 if args.limit is None else (args.limit or len(groups))
    shown_groups = groups[:cap]
    for item in shown_groups:
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
    table_lines = text.table(
        ["程序", "进程数", "占用", "提交", "窗口", "启动"],
        rows,
        ["left", "right", "right", "right", "left", "left"],
        limits=[28, 8, 10, 10, 6, 20],
    )
    for line in table_lines:
        print("  " + line)
    print("")
    note = _width_note(table_lines)
    if note:
        print(note)
    if len(groups) > len(shown_groups):
        # 截断了就要说。只给 3 行又不说，用户会以为这台机器就 3 个程序在占内存
        print(
            f"  · 只显示了占内存最多的 {len(shown_groups)} 个程序，"
            f"还有 {len(groups) - len(shown_groups)} 个没显示（--limit 0 看全部）"
        )
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
    # results 里是「没找到 / 匹配到多条」这类问题。以前只有一条都没找到时才打印，
    # 于是 `pcxray off 真的 不存在的` 只报成功那一条、退出码 0 —— 用户以为两个都办了。
    code = _print_results(results + done)
    if backup:
        # _print_results 结尾已经空了一行，这里再加一行就是连续两个空行
        print(f"  备份已写入：{text.safe(backup)}")
        print(f"  反悔就运行：pcxray restore \"{text.safe(backup)}\"")
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
    code = _print_results(results + done)
    if backup:
        print("")
        print(f"  备份已写入：{text.safe(backup)}")
    return code


def _pick(entries, names: list[str]) -> tuple[list[autostart.Entry], list[actions.Result]]:
    picked: list[autostart.Entry] = []
    problems: list[actions.Result] = []
    for name in names:
        if not (name or "").strip():
            # `pcxray off ""` 是引号没写对时最容易出现的输入。原来的提示是
            # 「✗ 　没有这一条」，名字那一段是空的，看不出是哪里错了。
            problems.append(actions.Result("（空名字）", False, "名字不能是空的，写要关的那一条的名字或 id"))
            continue
        hit, candidates = resolve(entries, name)
        if hit is not None:
            picked.append(hit)
            continue
        if candidates:
            # 一次列六条名字（有的名字六十多个字符）会变成一堵墙，看不完也抄不下。
            # 给前 4 条 + 一句去哪儿看全，比全列出来有用。
            shown = "、".join(f"{c.name}（{c.source}）" for c in candidates[:4])
            more = f"　…… 还有 {len(candidates) - 4} 条" if len(candidates) > 4 else ""
            problems.append(
                actions.Result(
                    name,
                    False,
                    f"匹配到 {len(candidates)} 条，请写更完整的名字：{shown}{more}"
                    f"（全名用 pcxray list --kind {candidates[0].kind} 看）",
                )
            )
        else:
            problems.append(
                actions.Result(
                    name,
                    False,
                    "没有这一条，先跑 pcxray list 看看名字"
                    "（如果是刚才用 pcxray 关掉的，`pcxray restore` 能放回去）",
                )
            )
    return picked, problems


def _cmd_restore(args) -> int:
    done, path = actions.restore(args.backup or "", dry_run=args.dry_run)
    code = _print_results(done)
    if path:
        # 还原失败时也写「使用备份」就是假话 —— 那份备份根本没被用上，是它
        # 本身有问题。这时换个说法，用户才知道要处理的是哪个文件。
        print("")
        # 注意：不能写成 f"...{"x" if c else "y"}..." —— 那是 3.12 才允许的
        # 同类引号嵌套，3.9 的解析器直接报 SyntaxError
        label = "使用备份" if any(r.ok for r in done) else "出问题的备份"
        print(f"  {label}：{text.safe(path)}")
    return code


def _cmd_backups(args) -> int:
    backups = actions.list_backups()
    if not backups:
        print("  还没有任何备份（说明你还没改过东西）")
        return 0
    shown = backups if args.all else backups[:10]
    for path in shown:
        # 只给路径没法挑：一堆 20261115-032457.json，谁知道哪份是刚才那次。
        # 顺手把时间和「改了几项」带上，一眼就能找到要还原的那一份。
        when, count = actions.describe_backup(path)
        print(f"  {os.path.basename(path):26s} {when}  {count}")
    if len(backups) > len(shown):
        # 一次打印三百行没人看得完，也没法用。默认只给最近的 10 份。
        print(f"  …… 还有 {len(backups) - len(shown)} 份更早的，用 pcxray backups --all 看全部")
    return 0


def _cmd_prune(args) -> int:
    if args.keep < 1:
        # 0 等于「全删」—— 备份全没了就没法反悔，这种事不能靠用户猜
        print("  ✗ --keep 至少是 1。全删备份等于放弃反悔的机会。", file=sys.stderr)
        return 2
    removed, kept, freed = actions.prune(args.keep, dry_run=args.dry_run)
    if not removed:
        print(f"  只有 {kept} 份备份，没超过 {args.keep} 份，不用清")
        return 0
    verb = "将要删掉" if args.dry_run else "已删掉"
    print(f"  {verb} {len(removed)} 份旧备份，留了 {kept} 份，释放 {text.mb(freed / 1048576)}")
    for path in removed[:5]:
        print(f"    {os.path.basename(path)}")
    if len(removed) > 5:
        print(f"    …… 还有 {len(removed) - 5} 份")
    if args.dry_run:
        print("  这是演练，什么都没删。去掉 --dry-run 才会真删。")
    return 0


def _cmd_web(args) -> int:
    from . import web

    # 70000 和 -1 以前会直接把 socket 的 OSError 抛出去，用户看到的是一屏
    # traceback。端口范围是可数的，说清楚比甩栈强。
    if not 0 <= args.port <= 65535:
        print(
            f"  ✗ --port 只能是 0–65535（0 = 让系统随便挑一个），收到 {args.port}",
            file=sys.stderr,
        )
        return 2
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
    # ram 的表格默认只看前 25 个（一屏能看完），list 默认全给。
    # 这里给 None 而不是 25：`--json` 要能区分「没写」和「写了 25」——
    # 脚本要的是全量，不该被表格的默认值截掉。
    p_ram.set_defaults(limit=None)
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
    p_backups.add_argument("--all", action="store_true", help="列出全部（默认只给最近 10 份）")
    p_backups.set_defaults(func=_cmd_backups)

    p_prune = sub.add_parser("prune", help="清掉旧备份，只留最近几份")
    p_prune.add_argument("--keep", type=int, default=20, help="留最近几份（默认 20）")
    p_prune.add_argument("--dry-run", action="store_true", help="只说要删哪些，不真删")
    p_prune.set_defaults(func=_cmd_prune)

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
        if winapi.elevate(argv):
            print("  已请求提权：在 UAC 弹窗里点「是」，结果会显示在新打开的窗口里。")
            print("  如果那个窗口一闪就没了，说明提权失败 —— 用普通权限重跑一次就行。")
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
