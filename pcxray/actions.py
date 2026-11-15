"""真的去改系统的部分 —— 整个项目里唯一会写东西的地方。

三条硬规矩：

1. **先备份，再动手。** 每次改动前把「原来是什么样」完整写进
   `~/.pcxray/backup/<时间>.json`，`restore` 能原样还原。备份写失败就不改。
2. **默认不碰系统组件。** 系统服务、微软的计划任务、RunOnce 一律拒绝，
   拒绝的理由直接展示给用户，不是静默跳过。
3. **关自启用 Windows 自己的开关。** 注册表项的状态存在
   `Explorer\\StartupApproved` 里（实测 12 字节：`02…` 启用、`04…` 禁用），
   改这个等于替用户在任务管理器里点了「禁用」，不删用户的数据。

关于验证的诚实说明：禁用标记要**重启**才能观察到效果，这台机器上不能重启，
所以 `disable` 只做到「写入后立刻读回来确认字节正确」。如果读完不一致会报错。
想立刻看到确定生效，用 `--mode move`（把 Run 值改名，当场可验证）。
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import autostart, safety

__all__ = [
    "Result",
    "disable",
    "enable",
    "restore",
    "list_backups",
    "BACKUP_DIR",
    "MODE_FLAG",
    "MODE_MOVE",
]

MODE_FLAG = "flag"  # 改 StartupApproved（和任务管理器一致）
MODE_MOVE = "move"  # 把 Run 值挪到 Windows 不读的 <键>-pcxray-disabled（当场可见）

BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".pcxray", "backup")

# 启用 / 禁用 的 12 字节标记。前 4 字节是状态，后 8 字节是时间（FILETIME）。
# 实测这台机器上：OneDrive 等启用项是 02000000 00000000 00000000，
# SecurityHealth / LVAPRCU / preinsassoguard 是 04000000 00000000 00000000。
_ENABLED = b"\x02\x00\x00\x00" + b"\x00" * 8
_DISABLED_PREFIX = b"\x03\x00\x00\x00"

# 老版本用「同一个键里改名成 ~pcxray~原名」来实现 --mode move。那其实拦不住
# 启动（Run 里的每个值 Windows 都会执行），只是为了读得懂老备份才留着这个名字。
MOVE_PREFIX = "~pcxray~"


class Result:
    __slots__ = ("name", "ok", "message")

    def __init__(self, name: str, ok: bool, message: str = ""):
        self.name = name
        self.ok = ok
        self.message = message

    def __repr__(self) -> str:
        return f"<{'ok' if self.ok else 'fail'} {self.name} {self.message}>"


def _disabled_now() -> bytes:
    """禁用标记：状态字节 + 当前时间的 FILETIME。"""
    ticks = int((time.time() + 11644473600) * 10_000_000)
    return _DISABLED_PREFIX + ticks.to_bytes(8, "little")


# ------------------------------------------------------------------ 注册表读写


def _hive(name: str):
    import winreg

    return {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}[name]


_APPROVED_KINDS = ("run", "run32", "runonce", "folder")


def _approved_subkey(kind: str) -> str:
    """`StartupApproved` 里对应哪一栏。

    只对注册表 Run 和启动文件夹有意义 —— 计划任务和服务是另外的开关机制，
    以前这里对它们也返回 `Run` 那一栏，等于给了一个会写错地方的后门。现在直接抛错，
    将来谁接错分支都会当场炸，而不是悄悄往 Run 栏里塞一条没用的记录。
    """
    if kind not in _APPROVED_KINDS:
        raise ValueError(f"{kind} 不走 StartupApproved 开关，别往 Run 栏写")
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
    if kind == "folder":
        return base + r"\StartupFolder"
    if kind == "run32":
        return base + r"\Run32"
    return base + r"\Run"


def _subkey_exists(hive: str, subkey: str) -> bool:
    """这个注册表位置在不在。写值的时候 Windows 会自动建键，还原不能这么干。"""
    import winreg

    try:
        root = _hive(hive)
    except KeyError:
        return False
    try:
        key = winreg.OpenKey(root, subkey, 0, winreg.KEY_READ)
        key.Close()
        return True
    except OSError:
        return False


def _read_value(hive: str, subkey: str, name: str) -> tuple[bool, object, int]:
    """返回 (存在?, 值, 类型)。"""
    import winreg

    try:
        key = winreg.OpenKey(_hive(hive), subkey)
    except OSError:
        return False, None, 0
    try:
        try:
            value, kind = winreg.QueryValueEx(key, name)
            return True, value, kind
        except OSError:
            return False, None, 0
    finally:
        key.Close()


def _write_value(hive: str, subkey: str, name: str, data, kind: int | None = None) -> None:
    """写一个值。`data` 是 bytes 时按 `kind`（默认 REG_BINARY）写，是 str 时按 REG_SZ 写。"""
    import winreg

    if kind is None:
        kind = winreg.REG_SZ if isinstance(data, str) else winreg.REG_BINARY
    key = winreg.CreateKeyEx(_hive(hive), subkey, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, name, 0, kind, data)
    finally:
        key.Close()


def _delete_value(hive: str, subkey: str, name: str) -> None:
    import winreg

    try:
        key = winreg.OpenKey(_hive(hive), subkey, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        winreg.DeleteValue(key, name)
    except OSError:
        pass
    finally:
        key.Close()


# ------------------------------------------------------------------ 备份


def _reserve_backup_path(stamp: str) -> str:
    """原子地占住一个不重名的备份文件名。

    以前是「先 `os.path.exists` 判重、再写」，两步之间没有原子性：网页上
    两个请求在同一秒到达时，都会看到「不存在」，然后抢同一个 `.tmp`，
    第二个直接 `WinError 32 另一个程序正在使用此文件` —— 用户看到的是
    「备份写不进去，已中止」，而实际上什么也没改。

    改成 `O_CREAT|O_EXCL`：创建成功才算占到，失败就换下一个序号。
    """
    suffix = 0
    while True:
        name = f"{stamp}.json" if suffix == 0 else f"{stamp}-{suffix}.json"
        path = os.path.join(BACKUP_DIR, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            suffix += 1
            if suffix > 999:
                raise OSError(f"备份文件名连续撞名一千次，放弃：{path}") from None
            continue
        os.close(fd)
        return path


def _save_backup(ops: list[dict]) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = _reserve_backup_path(stamp)
    payload = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": 1,
        "ops": ops,
    }
    # tmp 名字必须唯一，否则并发时两个线程写同一个 .tmp 又会撞车
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 半截的 tmp 不能留下，否则看起来像一份备份
        for leftover in (tmp,):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise
    return path


def list_backups() -> list[str]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted(
        (os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR) if n.endswith(".json")),
        reverse=True,
    )


def prune(keep: int, dry_run: bool = False) -> tuple[list[str], int, int]:
    """删掉旧备份，只留最近 `keep` 份。返回 (删掉的, 留下的份数, 释放的字节)。

    备份是无限增长的：每次改自启项都存一份，一个用了一年的机器上能攒出几百份
    `2026xxxx-xxxxxx.json`，全在用户主目录里，而且没有任何入口能清。只留最近的
    若干份就够反悔了 —— 真要回到很久以前，用户自己会留着文件。

    只删这个目录里的 `.json`，别的一概不碰。
    """
    backups = list_backups()  # 新的在前
    if keep < 1:
        raise ValueError("keep 必须 >= 1")
    if len(backups) <= keep:
        return [], len(backups), 0
    doomed = backups[keep:]
    removed: list[str] = []
    freed = 0
    for path in doomed:
        # 再核一遍：只删 BACKUP_DIR 里的 .json，别顺着软链接或相对路径跑出去
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(BACKUP_DIR):
            continue
        if not path.endswith(".json"):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if dry_run:
            removed.append(path)
            freed += size
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        removed.append(path)
        freed += size
    return removed, len(backups) - len(removed), freed


# ------------------------------------------------------------------ 单个条目


def _disabled_subkey(subkey: str) -> str:
    """`--mode move` 把值挪到这个键。Windows 不读它，所以真的不会启动。"""
    return subkey + autostart.DISABLED_SUFFIX


def _original_subkey(subkey: str) -> str:
    """被挪走的键 → 原来那个键。"""
    suffix = autostart.DISABLED_SUFFIX
    return subkey[: -len(suffix)] if subkey.endswith(suffix) else subkey


def _plan(entry, turn_on: bool, mode: str) -> dict | None:
    """算出「要改什么」和「原来是什么」，不改任何东西。

    备份和真正执行都用这一份计划，避免两处逻辑走偏。
    """
    locator = entry.locator or {}
    if entry.kind in ("run", "run32", "folder", "runonce"):
        hive = locator.get("hive", "HKCU")
        subkey = locator.get("subkey", "")
        value = locator.get("value", entry.name)
        if not subkey:
            return None
        if mode == MODE_MOVE:
            if entry.kind == "folder":
                # 启动文件夹里是一个个文件，没有「挪注册表值」这回事。说清楚该用哪种
                # 模式，别只丢一句「不知道怎么改这一条」。
                return {
                    "name": entry.name,
                    "refuse": "启动文件夹的项没法用 --mode move，用默认的标记模式（不加 --mode move）",
                }
            # 把值从「Windows 会读的键」挪到「Windows 不读的键」。
            # 以前是同一个键里把名字改成 `~pcxray~xxx` —— 但 Run 里的**每个**值
            # Windows 都会执行，改名字拦不住它，那个「立刻生效」是假的。
            moved = bool(locator.get("moved"))
            if turn_on:
                if not moved:
                    return {"name": entry.name, "noop": True}  # 本来就在 Run 里
                source_key, target_key = subkey, _original_subkey(subkey)
            else:
                if moved:
                    return {"name": entry.name, "noop": True}  # 已经挪走了
                source_key, target_key = subkey, _disabled_subkey(subkey)
            existed, old_value, old_kind = _read_value(hive, source_key, value)
            if not existed:
                # 源不存在就没得挪。继续往下走会「把目标写成空」，等于删掉它
                return {
                    "name": entry.name,
                    "refuse": f"注册表里已经没有 {value} 这一条了，可能被别处改过",
                }
            # 目标键里已经有同名的值 —— 再写就是覆盖，用户完全看不到被覆盖掉的是
            # 什么。宁可拒绝，也不要悄悄毁掉一条自启项。
            target_exists, target_value, _target_kind = _read_value(hive, target_key, value)
            if target_exists and target_value != old_value:
                return {
                    "name": entry.name,
                    "refuse": f"{target_key} 里已经有一条同名的值，再挪过去会覆盖掉它，所以没动",
                }
            return {
                "kind": "move",
                "hive": hive,
                "subkey": source_key,
                "target_subkey": target_key,
                "old_name": value,
                "new_name": value,
                "existed": existed,
                "value": old_value if isinstance(old_value, str) else "",
                "value_kind": old_kind,
                "name": entry.name,
            }
        approved = _approved_subkey(entry.kind)
        existed, old_value, old_kind = _read_value(hive, approved, value)
        # 老值可能不是 bytes —— 本机 HKCU 里真有一条 REG_SZ 的 StartupApproved
        # 值（GalaxyClient，空字符串）。只记 hex 会把它丢掉，还原时就变成
        # 「删掉这条记录」，和「恢复成原来的空字符串」不是一回事。
        old_hex = old_value.hex() if isinstance(old_value, (bytes, bytearray)) else None
        old_str = old_value if isinstance(old_value, str) else None
        # 已经是目标状态就别再写一遍。重复 `off` 一条本来就关着的项，以前会
        # 又写一份备份、又报「已写入开关标记」—— 用户会以为刚才那次没生效，
        # 备份目录也跟着长胖。
        first = (old_hex or "")[:2]
        if turn_on:
            already = (not existed) or first in ("02", "06")
        else:
            already = existed and first in ("03", "04")
        return {
            "kind": "flag",
            "noop": already,
            "hive": hive,
            "subkey": approved,
            "value_name": value,
            # 自启项本身在哪个键。还原时拿它判断「这一条还在不在」——
            # 光看 StartupApproved 里的标记是不够的，那正是我们要写回去的东西。
            "target_subkey": subkey,
            "existed": existed,
            "old": old_hex,
            "old_str": old_str,
            "old_kind": old_kind,
            "new": (_ENABLED if turn_on else _disabled_now()).hex(),
            "name": entry.name,
        }
    if entry.kind == "task":
        return {
            "kind": "task",
            "path": locator.get("path", "\\"),
            "task": locator.get("name", entry.name),
            "was_enabled": entry.enabled,
            "name": entry.name,
        }
    if entry.kind == "service":
        return {
            "kind": "service",
            "service": locator.get("name", entry.name),
            "was_enabled": entry.enabled,
            "name": entry.name,
        }
    return None


def _set_task(path: str, task: str, turn_on: bool) -> tuple[bool, str]:
    """启 / 停计划任务，并读回状态确认。返回 (成功?, 给人看的话)。

    `_apply` 和 `restore` 都走这里 —— 第一版两处各写了一遍，结果修了「关」
    忘了修「还原」，还原时又报同一个假失败。同一条逻辑只能有一份。
    """
    from . import pwsh

    verb = "Enable" if turn_on else "Disable"
    want = "Ready" if turn_on else "Disabled"
    path_q = pwsh.ps_quote(path)
    task_q = pwsh.ps_quote(task)
    script = (
        f"{verb}-ScheduledTask -TaskPath {path_q} -TaskName {task_q} -ErrorAction Stop | Out-Null;"
        f"(Get-ScheduledTask -TaskPath {path_q} -TaskName {task_q}).State"
    )
    ok, out, reason = pwsh.run_text(script, timeout=60)
    if not ok:
        return False, reason or "改计划任务失败"
    # 改完立刻读回状态。计划任务的状态是权威的，不用等重启。
    state = out.splitlines()[-1].strip() if out else ""
    if state.lower() != want.lower():
        return False, f"改完读回来的状态是「{state or '空'}」，不是「{want}」"
    return True, f"已{'启用' if turn_on else '禁用'}计划任务（状态：{state}）"


def _set_service(name: str, turn_on: bool) -> tuple[bool, str]:
    """把服务改成自动 / 手动启动，并读回启动方式确认。"""
    from . import pwsh

    want = "Automatic" if turn_on else "Manual"
    name_q = pwsh.ps_quote(name)
    script = (
        f"Set-Service -Name {name_q} -StartupType {want} -ErrorAction Stop;"
        f"(Get-Service -Name {name_q}).StartType.ToString()"
    )
    ok, out, reason = pwsh.run_text(script, timeout=60)
    if not ok:
        return False, reason or f"改成 {want} 失败"
    state = out.splitlines()[-1].strip() if out else ""
    if state.lower() != want.lower():
        return False, f"改完读回来的启动方式是「{state or '空'}」，不是「{want}」"
    return True, f"已改成{'自动' if turn_on else '手动'}启动（服务不会立刻停止，重启后生效）"


def _apply(plan: dict, turn_on: bool) -> Result:
    kind = plan["kind"]
    if kind == "flag":
        hive, subkey, value_name = plan["hive"], plan["subkey"], plan["value_name"]
        data = _ENABLED if turn_on else _disabled_now()
        try:
            _write_value(hive, subkey, value_name, data)
        except PermissionError:
            return Result(plan["name"], False, "没有权限写这个键，需要管理员身份运行")
        except OSError as exc:
            return Result(plan["name"], False, f"写注册表失败：{exc}")
        # 写完立刻读回来确认。读不回来等于没改。
        existed, got, _kind = _read_value(hive, subkey, value_name)
        if not existed or not isinstance(got, (bytes, bytearray)) or bytes(got) != data:
            return Result(plan["name"], False, "写入后读回来的值和预期不一致，已中止")
        return Result(plan["name"], True, "已写入开关标记")

    if kind == "move":
        hive, source_key = plan["hive"], plan["subkey"]
        target_key = plan.get("target_subkey") or source_key
        if not plan["existed"]:
            # 源值不在，就没有东西可以挪。报成功会让用户以为办成了，其实什么都没发生。
            return Result(plan["name"], False, f"找不到 {plan['old_name']}，可能已经被改过或删掉了")
        try:
            _write_value(
                hive, target_key, plan["new_name"], plan["value"], plan["value_kind"] or None
            )
            _delete_value(hive, source_key, plan["old_name"])
        except PermissionError:
            return Result(plan["name"], False, "没有权限改这个键，需要管理员身份运行")
        except OSError as exc:
            return Result(plan["name"], False, f"改注册表失败：{exc}")
        # 和 flag 一样，改完读回来确认：源必须没了，目标必须有且值一样
        still, _v, _k = _read_value(hive, source_key, plan["old_name"])
        if still:
            return Result(plan["name"], False, f"挪走之后 {plan['old_name']} 还在原来的键里，已中止")
        moved, got, _kind2 = _read_value(hive, target_key, plan["new_name"])
        if not moved or got != plan["value"]:
            return Result(plan["name"], False, "挪过去之后读回来的值和预期不一致，已中止")
        where = "不会启动的键" if not turn_on else "原来的键"
        return Result(plan["name"], True, f"已挪到{where}（{target_key}）")

    if kind == "task":
        ok, message = _set_task(plan["path"], plan["task"], turn_on)
        return Result(plan["name"], ok, message)

    if kind == "service":
        ok, message = _set_service(plan["service"], turn_on)
        return Result(plan["name"], ok, message)
    return Result(plan.get("name", "?"), False, "不认识的条目类型")


# ------------------------------------------------------------------ 对外接口


def _run(items, turn_on: bool, force: bool, dry_run: bool, mode: str) -> tuple[list[Result], str]:
    results: list[Result] = []
    plans: list[dict] = []
    skipped: list[Result] = []
    already: list[Result] = []
    seen: set[str] = set()
    admin = _is_admin()

    for entry in items:
        if entry.key in seen:
            continue  # 同一个名字传了两遍，不要重复改、也不要重复记备份
        seen.add(entry.key)
        # 系统组件一律不动，--force 也不行。放行它等于给「关掉系统服务导致开不了机」
        # 开了个后门，而这个工具的目的不是让用户把电脑搞坏。
        if entry.protected:
            skipped.append(
                Result(entry.name, False, "跳过：系统自带组件，关掉可能影响开机、网络或安全防护")
            )
            continue
        reason = safety.refuse_reason(entry.kind, entry.scope, entry.name, False, admin)
        if reason:
            skipped.append(Result(entry.name, False, f"跳过：{reason}"))
            continue
        if safety.needs_force(entry.kind) and not force:
            skipped.append(
                Result(
                    entry.name,
                    False,
                    "跳过：服务关错会开不了机，确认要改请加 --force",
                )
            )
            continue
        plan = _plan(entry, turn_on, mode)
        if plan is None:
            skipped.append(Result(entry.name, False, "跳过：不知道怎么改这一条"))
            continue
        if plan.get("refuse"):
            skipped.append(Result(entry.name, False, f"跳过：{plan['refuse']}"))
            continue
        if plan.get("noop"):
            already.append(
                Result(entry.name, True, "本来就是开着的，没动" if turn_on else "本来就是关着的，没动")
            )
            continue
        plans.append(plan)

    if not plans:
        return already + skipped, ""

    if dry_run:
        for plan in plans:
            results.append(Result(plan["name"], True, f"（演练）将要{_describe(plan, turn_on)}"))
        return results + already + skipped, ""

    try:
        backup = _save_backup(plans)
    except OSError as exc:
        # 备份失败就绝不改系统 —— 没有退路的事情不做
        return already + skipped + [Result("（全部）", False, f"备份写不进去，已中止：{exc}")], ""

    for plan in plans:
        results.append(_apply(plan, turn_on))
    return results + already + skipped, backup


def _describe(plan: dict, turn_on: bool) -> str:
    verb = "启用" if turn_on else "禁用"
    if plan["kind"] == "flag":
        return f"{verb}（写 {plan['subkey'].split(chr(92))[-1]} 开关）"
    if plan["kind"] == "move":
        target = plan.get("target_subkey") or ""
        where = target.split(chr(92))[-1] if target else "同一个键"
        if target and target == plan["subkey"]:
            # 老备份里记的是同一个键里改名
            return f"{verb}（把值改名为 {plan['new_name']}）"
        return f"{verb}（把 {plan.get('old_name') or plan['new_name']} 挪到 {where}）"
    if plan["kind"] == "task":
        return f"{verb}计划任务"
    if plan["kind"] == "service":
        return f"把服务改成{'自动' if turn_on else '手动'}启动"
    return verb


def _is_admin() -> bool:
    from . import winapi

    return winapi.is_admin()


def disable(items, force: bool = False, dry_run: bool = False, mode: str = MODE_FLAG):
    return _run(items, False, force, dry_run, mode)


def enable(items, force: bool = False, dry_run: bool = False, mode: str = MODE_FLAG):
    return _run(items, True, force, dry_run, mode)


def describe_backup(path: str) -> tuple[str, str]:
    """备份的「什么时候、改了几项」，读不出来就老实说不知道。

    列表里只有一串 `20261115-032457.json`，用户没法判断哪份是刚才那次操作 ——
    还原错一份比不还原更糟。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return "时间未知", "内容读不出来"
    created = payload.get("created") or ""
    ops = payload.get("ops")
    count = f"{len(ops)} 项" if isinstance(ops, list) else "结构不对"
    if isinstance(created, str) and len(created) >= 19:
        created = created[:19].replace("T", " ")
    return created or "时间未知", count


def _value_exists(hive: str, subkey: str, value_name: str) -> bool:
    """那个值还在不在。

    读不到（权限、键不存在）一律当「还在」—— 宁可多写一个无害的标记，也不能
    因为查不了就把用户真正要还原的东西跳过。
    """
    try:
        import winreg

        with winreg.OpenKey(_hive(hive), subkey) as key:
            winreg.QueryValueEx(key, value_name)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _backup_is_usable(path: str) -> bool:
    """这份备份能不能拿来还原。

    只看结构，不动任何东西 —— 用来在「用最近一份还原」时跳过坏文件。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    ops = payload.get("ops")
    if not isinstance(ops, list) or not ops:
        return False
    return all(isinstance(op, dict) for op in ops)


def restore(path: str = "", dry_run: bool = False) -> tuple[list[Result], str]:
    """按备份还原。path 为空时用最近的一份。"""
    skipped: list[str] = []
    if not path:
        backups = list_backups()
        if not backups:
            return [Result("（还原）", False, "没有任何备份文件")], ""
        # 最新的那份可能是坏的：写到一半断电、磁盘满了、被别的东西占用了名字。
        # 只认「读得出来、结构对、里面有 ops」的，不然用户敲 `pcxray restore`
        # 只会得到一句「结构不对」，明明前面还有好几份好的。
        path = ""
        for candidate in backups:
            if _backup_is_usable(candidate):
                path = candidate
                break
            skipped.append(os.path.basename(candidate))
        if not path:
            return [
                Result(
                    "（还原）",
                    False,
                    f"最近的 {len(backups)} 份备份都读不出来，没法还原",
                )
            ], ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return [Result("（还原）", False, f"没有这个备份文件：{path}")], path
    except OSError as exc:
        return [Result("（还原）", False, f"读备份失败：{path}（{exc.strerror or exc}）")], path
    except json.JSONDecodeError:
        return [Result("（还原）", False, f"备份文件不是合法 JSON：{path}")], path
    ops = payload.get("ops") or []
    # 备份文件可能是坏的：手改过、写到一半断电、从别的机器拷来的。以前这里
    # 直接 `op.get(...)`，`{"ops": "x"}` 会抛 AttributeError 打出一屏 traceback；
    # `{}` 更糟 —— 返回空结果，命令行一声不响地退出 0，用户以为还原成功了。
    if not isinstance(ops, list) or any(not isinstance(op, dict) for op in ops):
        return [Result("（还原）", False, f"备份文件的结构不对，没法还原：{path}")], path
    if not ops:
        return [Result("（还原）", False, f"这份备份里没有任何要还原的东西：{path}")], path
    results: list[Result] = []
    version = payload.get("version")
    if version not in (None, 1):
        # 未来的格式可能多出这一版不认识的字段。硬按 v1 的规矩还原，等于拿
        # 一份看不懂的说明书去改用户的注册表 —— 至少要说一声。
        results.append(
            Result("（还原）", True, f"这份备份标的是 v{version} 格式，这个版本只认 v1，可能读不全")
        )
    known = ("flag", "move", "task", "service")
    usable = [op for op in ops if op.get("kind") in known]
    unknown = [op for op in ops if op.get("kind") not in known]
    if not usable:
        # 一条都认不出来就别报告「已还原」—— 用户会以为注册表回去了
        kinds = sorted({str(op.get("kind") or "(缺失)") for op in unknown})
        detail = f"（{len(unknown)} 条不认识的格式：{'、'.join(kinds)}）" if unknown else ""
        return results + [
            Result("（还原）", False, f"这份备份里没有这个版本能还原的内容{detail}，没法还原：{path}")
        ], path
    for op in unknown:
        results.append(
            Result(
                op.get("name", "?"),
                False,
                f"这一条的格式（kind={op.get('kind')}）这个版本不认识，跳过了",
            )
        )
    if skipped:
        # 说清楚「用的是哪一份、跳过了哪些」，不然用户会以为还原的是最新的
        results.append(
            Result(
                "（还原）",
                True,
                f"跳过了 {len(skipped)} 份读不出来的备份：{'、'.join(skipped[:3])}"
                + ("……" if len(skipped) > 3 else ""),
            )
        )
    for op in usable:
        name = op.get("name", "?")
        if dry_run:
            results.append(Result(name, True, "（演练）将还原"))
            continue
        try:
            if (
                op.get("kind") == "flag"
                # 老值是字符串的时候照旧还原：那是「把用户原来的数据放回去」，
                # 不是我们写的那种开关标记，不能因为自启项没了就把它丢掉。
                and op.get("old_str") is None
                and op.get("target_subkey")
                and not _value_exists(op.get("hive", ""), op["target_subkey"], op.get("value_name", ""))
            ):
                # 自启项已经被删了（用户自己删的、卸载程序清掉的）。再往
                # StartupApproved 里写一个标记，只会在注册表里留一条谁都看不见的
                # 垃圾 —— 它对应的自启项根本不存在了。
                results.append(Result(name, True, "这一条已经不在了，跳过"))
                continue
            if op["kind"] in ("flag", "move") and not _subkey_exists(op.get("hive", ""), op.get("subkey", "")):
                # 备份可能是从别的机器拷来的，或者那一片注册表已经被删了。
                # 直接写下去 Windows 会**新建**这个键，然后我们报告「已还原」——
                # 等于凭空在用户的注册表里造了一个位置。宁可报失败。
                results.append(
                    Result(name, False, f"这台机器上没有这个注册表位置：{op.get('subkey', '')}")
                )
                continue
            if op["kind"] == "flag":
                hive, subkey = op["hive"], op["subkey"]
                old_hex = op.get("old")
                old_str = op.get("old_str")
                if not op.get("existed"):
                    _delete_value(hive, subkey, op["value_name"])
                    results.append(Result(name, True, "已删除开关标记（恢复成默认启用）"))
                elif old_hex is not None:
                    _write_value(hive, subkey, op["value_name"], bytes.fromhex(old_hex))
                    # 还原也要读回来确认 —— 这是安全网，不能只喊一句「已还原」
                    got_ok, got, _k = _read_value(hive, subkey, op["value_name"])
                    if not got_ok or not isinstance(got, (bytes, bytearray)) or bytes(got).hex() != old_hex:
                        results.append(Result(name, False, "还原后读回来的字节和备份不一致"))
                    else:
                        results.append(Result(name, True, "已还原开关标记"))
                elif old_str is not None:
                    # 原来是字符串类型的值（少见但真有），按原类型写回去
                    _write_value(hive, subkey, op["value_name"], old_str, op.get("old_kind") or None)
                    got_ok, got, _k = _read_value(hive, subkey, op["value_name"])
                    if not got_ok or got != old_str:
                        results.append(Result(name, False, "还原后读回来的值和备份不一致"))
                    else:
                        results.append(Result(name, True, "已还原开关标记（原值不是二进制）"))
                else:
                    _delete_value(hive, subkey, op["value_name"])
                    results.append(Result(name, True, "已删除开关标记（恢复成默认启用）"))
            elif op["kind"] == "move":
                hive, subkey = op["hive"], op["subkey"]
                target = op.get("target_subkey") or subkey
                if target != subkey:
                    # 新式备份：值被挪到了另一个键，放回原来的键
                    _write_value(
                        hive, subkey, op["old_name"], op["value"], op.get("value_kind") or None
                    )
                    _delete_value(hive, target, op["new_name"])
                else:
                    # 老备份：同一个键里改名（那时候的做法其实拦不住启动）
                    _delete_value(hive, subkey, op["new_name"])
                    if op.get("existed"):
                        _write_value(
                            hive, subkey, op["old_name"], op["value"], op.get("value_kind") or None
                        )
                got_ok, got, _k = _read_value(hive, subkey, op["old_name"])
                if not got_ok or got != op["value"]:
                    results.append(Result(name, False, "还原后读回来的值和备份不一致"))
                else:
                    results.append(Result(name, True, "已放回原来的键"))
            elif op["kind"] == "task":
                if op.get("was_enabled"):
                    ok, message = _set_task(op.get("path", "\\"), op["task"], True)
                    results.append(Result(name, ok, message))
                else:
                    results.append(Result(name, True, "原本就是禁用状态，未改动"))
            elif op["kind"] == "service":
                if op.get("was_enabled"):
                    ok, message = _set_service(op["service"], True)
                    results.append(Result(name, ok, message))
                else:
                    results.append(Result(name, True, "原本就是手动启动，未改动"))
            else:
                results.append(Result(name, False, f"不认识的备份记录：{op.get('kind')}"))
        except PermissionError:
            results.append(Result(name, False, "没有权限，需要管理员身份运行"))
        except OSError as exc:
            results.append(Result(name, False, f"还原失败：{exc}"))
    return results, path
