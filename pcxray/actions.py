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

from . import safety

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
MODE_MOVE = "move"  # 把 Run 值改名成 ~pcxray~<原名>（当场可见）

BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".pcxray", "backup")

# 启用 / 禁用 的 12 字节标记。前 4 字节是状态，后 8 字节是时间（FILETIME）。
# 实测这台机器上：OneDrive 等启用项是 02000000 00000000 00000000，
# SecurityHealth / LVAPRCU / preinsassoguard 是 04000000 00000000 00000000。
_ENABLED = b"\x02\x00\x00\x00" + b"\x00" * 8
_DISABLED_PREFIX = b"\x03\x00\x00\x00"

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


# ------------------------------------------------------------------ 单个条目


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
                return None  # 启动文件夹的项没有「改名的值」这回事
            source = MOVE_PREFIX + value if turn_on else value
            target = value if turn_on else MOVE_PREFIX + value
            existed, old_value, old_kind = _read_value(hive, subkey, source)
            if not existed:
                # 源不存在就没得改。继续往下走会「把 target 写成空」，等于删掉它
                return {"name": entry.name, "refuse": f"注册表里已经没有 {source} 这一条了，可能被别处改过" }
            # 目标名已经有值了 —— 再写就是覆盖，而且用户完全看不到被覆盖掉的是
            # 什么。宁可拒绝，也不要悄悄毁掉一条自启项。
            target_exists, target_value, _target_kind = _read_value(hive, subkey, target)
            if target_exists and target_value != old_value:
                # 覆盖 = 悄悄毁掉一条用户的自启项，而且事后看不出来
                return {
                    "name": entry.name,
                    "refuse": f"{target} 已经存在，再改会覆盖掉它，所以没动",
                }
            return {
                "kind": "move",
                "hive": hive,
                "subkey": subkey,
                "old_name": source,
                "new_name": target,
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
        return {
            "kind": "flag",
            "hive": hive,
            "subkey": approved,
            "value_name": value,
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
        hive, subkey = plan["hive"], plan["subkey"]
        try:
            if turn_on:
                if not plan["existed"]:
                    # 源值不在，就没有东西可以改回去。报成功会让用户以为
                    # 恢复了，其实什么都没发生。
                    return Result(
                        plan["name"], False, f"找不到 {plan['old_name']}，可能已经改回去了"
                    )
                _delete_value(hive, subkey, plan["old_name"])
                _write_value(
                    hive, subkey, plan["new_name"], plan["value"], plan["value_kind"] or None
                )
            else:
                if not plan["existed"]:
                    return Result(plan["name"], False, "找不到这条注册表值，可能已经被删掉了")
                _delete_value(hive, subkey, plan["old_name"])
                _write_value(
                    hive, subkey, plan["new_name"], plan["value"], plan["value_kind"] or None
                )
        except PermissionError:
            return Result(plan["name"], False, "没有权限改这个键，需要管理员身份运行")
        except OSError as exc:
            return Result(plan["name"], False, f"改注册表失败：{exc}")
        # 和 flag 一样，改完读回来确认
        moved, _v, _k = _read_value(hive, subkey, plan["new_name"])
        if not moved:
            return Result(plan["name"], False, f"改名后找不到 {plan['new_name']}，已中止")
        return Result(plan["name"], True, f"已{'恢复' if turn_on else '改名'}为 {plan['new_name']}")

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
        plans.append(plan)

    if not plans:
        return skipped, ""

    if dry_run:
        for plan in plans:
            results.append(Result(plan["name"], True, f"（演练）将要{_describe(plan, turn_on)}"))
        return results + skipped, ""

    try:
        backup = _save_backup(plans)
    except OSError as exc:
        # 备份失败就绝不改系统 —— 没有退路的事情不做
        return skipped + [Result("（全部）", False, f"备份写不进去，已中止：{exc}")], ""

    for plan in plans:
        results.append(_apply(plan, turn_on))
    return results + skipped, backup


def _describe(plan: dict, turn_on: bool) -> str:
    verb = "启用" if turn_on else "禁用"
    if plan["kind"] == "flag":
        return f"{verb}（写 {plan['subkey'].split(chr(92))[-1]} 开关）"
    if plan["kind"] == "move":
        return f"{verb}（把值改名为 {plan['new_name']}）"
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


def restore(path: str = "", dry_run: bool = False) -> tuple[list[Result], str]:
    """按备份还原。path 为空时用最近的一份。"""
    if not path:
        backups = list_backups()
        if not backups:
            return [Result("（还原）", False, "没有任何备份文件")], ""
        path = backups[0]
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
    for op in ops:
        name = op.get("name", "?")
        if dry_run:
            results.append(Result(name, True, "（演练）将还原"))
            continue
        try:
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
                _delete_value(hive, subkey, op["new_name"])
                if op.get("existed"):
                    _write_value(
                        hive, subkey, op["old_name"], op["value"], op.get("value_kind") or None
                    )
                results.append(Result(name, True, "已改回原来的值名"))
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
