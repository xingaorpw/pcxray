"""端到端自检：拿真的命令行和真的 HTTP 服务跑一遍。

单元测试保证逻辑对，这个脚本保证**装完之后命令行的行为**对。CI 里跑它，
本地也能跑：

    python scripts/ci_checks.py

为什么不用一堆 `python -c` 单行命令塞进 workflow：那些东西本地没法复用，
引号还会被 YAML 和 PowerShell 来回吃掉，改一次错一次。
"""

from __future__ import annotations

import glob
import json
import os
import tempfile
import shutil
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request

# 直接 `python scripts/ci_checks.py` 时也能 import 到本仓库的 pcxray
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f"　{detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def run(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pcxray"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


def check_bad_arguments() -> None:
    """坏参数必须当场报错，不能静默给一份「这一类没有条目」的报告。"""
    for args in (
        ["list", "--kind", "nope"],
        ["list", "--include", "nope"],
        ["list", "--limit", "-1"],
    ):
        code, _out, err = run(args)
        check(f"{' '.join(args)} 退出码是 2", code == 2, f"实际 {code}")
        first = next((ln.strip() for ln in err.splitlines() if ln.strip()), "")
        check(f"{' '.join(args)} 有说明", "✗" in first, first or "stderr 是空的")


def check_json_filter() -> None:
    """`--json --kind X` 只能给 X，而且计数要跟条目对得上。"""
    code, out, err = run(["list", "--json", "--kind", "task"])
    if code != 0:
        check("list --json --kind task 成功", False, err.strip()[:80])
        return
    data = json.loads(out)["autostart"]
    items = data["items"]
    check("--kind 过滤真的生效", all(i["kind"] == "task" for i in items), f"{len(items)} 条")
    check("total 等于条目数", data["total"] == len(items), f"{data['total']} vs {len(items)}")
    check(
        "启用 + 禁用 == 总数",
        data["enabled"] + data["disabled"] == data["total"],
        f"{data['enabled']}+{data['disabled']}",
    )

    code, out, _err = run(["list", "--json", "--limit", "3"])
    limited = json.loads(out)["autostart"]
    # total 是命中数，items 才是给出来的那一段；两者之差写在 hidden_by_limit
    check(
        "--limit 真的截断",
        len(limited["items"]) == 3 and limited["hidden_by_limit"] == limited["total"] - 3,
        f"items={len(limited['items'])} total={limited['total']} hidden={limited['hidden_by_limit']}",
    )

    code, out, _err = run(["ram", "--json"])
    ram = json.loads(out)
    total = round(sum(g["mb"] for g in ram["groups"]), 1)
    check("ram 合计等于各组相加", abs(ram["total_mb"] - total) < 0.05, f"{ram['total_mb']} vs {total}")


def check_web_csrf() -> None:
    """网页接口必须拒绝没有令牌的 POST。

    自启项的 key 是公开算法算出来的，所以「只绑 127.0.0.1」挡不住同机浏览器里
    的任意网页 —— 没有令牌校验就等于谁都能关你的自启项。
    """
    from pcxray import web

    server = web.make_server(0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/api/report", timeout=120) as resp:
            report = json.loads(resp.read())
        check("GET /api/report 正常", "autostart" in report, f"{report.get('process_count')} 个进程")

        with urllib.request.urlopen(base + "/", timeout=30) as resp:
            page = resp.read().decode("utf-8")
        check("页面里带着令牌", "__TOKEN__" not in page and len(server.token) >= 16)

        body = json.dumps({"keys": ["deadbeef1234"]}).encode()
        req = urllib.request.Request(
            base + "/api/off",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        check("没有令牌的 POST 被拒（403）", code == 403, f"实际 {code}")

        req = urllib.request.Request(
            base + "/api/off",
            data=body,
            headers={"Content-Type": "application/json", "X-PCXRAY-Token": server.token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        # 带着正确令牌但 key 不存在 → 400（找到了才动手）
        check("带令牌但条目不存在是 400", code == 400, f"实际 {code}")
    finally:
        server.shutdown()


def check_json_contract() -> None:
    """JSON 的计数必须自洽，截断必须说出来。

    `--json --limit N` 以前把 total 也改成 N，脚本看到的是一个「只有 N 条自启
    项」的世界。现在 total 是命中数，items 是给出来的，两者之差写在
    hidden_by_limit 里。
    """
    import json
    import subprocess
    import sys

    for args, expect_hidden in (
        (["--json", "--limit", "3"], True),
        (["--json"], False),
        (["--json", "--kind", "task", "--limit", "2"], True),
    ):
        done = subprocess.run(
            [sys.executable, "-m", "pcxray", "list"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if done.returncode != 0:
            check(f"{' '.join(args)} 能跑", False, done.stderr[-200:])
            continue
        try:
            auto = json.loads(done.stdout)["autostart"]
        except (json.JSONDecodeError, KeyError) as exc:
            check(f"{' '.join(args)} 是合法 JSON", False, str(exc))
            continue
        consistent = (
            len(auto["items"]) + auto["hidden_by_limit"] == auto["total"]
            and auto["enabled"] + auto["disabled"] == auto["total"]
        )
        check(f"{' '.join(args)} 计数自洽", consistent, str(auto.get("total")))
        if expect_hidden:
            check(f"{' '.join(args)} 说了截断", auto["hidden_by_limit"] > 0, str(auto.get("hidden_by_limit")))


def check_no_handle_leak() -> None:
    """反复扫描不能漏句柄。

    这条只有真机上量得出来：每个进程开一个 `OpenProcess`、再开一个快照，漏一个
    短时间看不出来，跑久了就是「用着用着打不开新进程」。预热一轮把 DLL 的一次性
    开销吃掉，再连续跑，句柄数应该纹丝不动。
    """
    import ctypes

    from pcxray import autostart, report, winapi

    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]

    def handles() -> int:
        count = ctypes.c_uint32()
        k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(count))
        return count.value

    entries, _problems = autostart.scan()
    process_list = winapi.processes()
    report.build(entries=entries, process_list=process_list)  # 预热
    base = handles()
    for _ in range(3):
        report.build(entries=entries, process_list=winapi.processes())
    grown = handles() - base
    # 留 2 个余量：PowerShell 子进程偶尔会把标准句柄带进来
    check("反复扫描不漏句柄", grown <= 2, f"多了 {grown} 个")


def check_page_markup() -> None:
    """网页的 HTML 结构必须闭合，占位符必须被替换掉。

    没有视觉通道也能查这个：标签不平衡、`__TOKEN__` 没替换，浏览器那边就是
    一片空白或者 403，而接口测试照样全绿。
    """
    import html.parser

    from pcxray import web

    page = web._PAGE.replace("__VERSION__", "0.2.0").replace("__TOKEN__", "x" * 32)
    void = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    class Checker(html.parser.HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.problems: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag not in void:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in void:
                return
            if not self.stack:
                self.problems.append(f"多余的 </{tag}>")
                return
            if self.stack[-1] != tag:
                self.problems.append(f"</{tag}> 关的是 <{self.stack[-1]}>")
                if tag in self.stack:
                    while self.stack and self.stack.pop() != tag:
                        pass
                return
            self.stack.pop()

    checker = Checker()
    checker.feed(page)
    check("页面标签都闭合", not checker.stack and not checker.problems,
          str(checker.problems[:3] or checker.stack[:3]))
    check("没有占位符残留", "__TOKEN__" not in page and "__VERSION__" not in page)
    check("页面里有脚本和样式", "<script>" in page and "<style>" in page)


def _run_page_js(payload: dict, tail: str, fetch_js: str = "") -> dict | None:
    """把页面里那段 JS 放到 node 里跑，返回它 `console.log` 的 JSON。

    静态检查只能证明「`esc` 函数存在」，证明不了「每个插值都调了它」，也证明
    不了「筛选逻辑对不对」。这里跑的是浏览器真正会执行的那份代码。
    """
    from pcxray import web

    node = shutil.which("node")
    if not node:
        return None
    match = re.search(r"<script>(.*?)</script>", web._PAGE.replace("__TOKEN__", "t"), re.S)
    if not match:
        return {}
    harness = (
        "const els={};"
        "function el(id){if(!els[id])els[id]={innerHTML:'',textContent:'',"
        "classList:{add(){},remove(){}},onclick:null,dataset:{}};return els[id];}"
        "global.document={getElementById:el,querySelectorAll:()=>[]};"
        + ("global.fetch=" + fetch_js + ";" if fetch_js else "global.fetch=async()=>{throw new Error('不该发请求')};")
        + 
        "global.confirm=()=>true;\n"
        + match.group(1)
        + "\nDATA = "
        + json.dumps(payload, ensure_ascii=False)
        + ";"
        + tail
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "page.js")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(harness)
        proc = subprocess.run(
            [node, path], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
    if proc.returncode != 0:
        return {"__error__": proc.stderr.strip()[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"__error__": proc.stdout.strip()[:300]}


def _item(name: str, kind: str, **extra) -> dict:
    base = {
        "key": name,
        "name": name,
        "kind": kind,
        "command": r"C:\x.exe",
        "detail": "",
        "source": "注册表 · 用户 Run",
        "company": "某公司",
        "enabled": True,
        "can_disable": True,
        "needs_force": False,
        "refuse": "",
        "flags": [],
        "running_mb": 0,
        "exe": r"C:\x.exe",
    }
    base.update(extra)
    return base


def _payload(items: list[dict], **extra) -> dict:
    data = {
        "generated": "2026-01-01 00:00:00",
        "process_count": 1,
        "problems": [],
        # 键名必须和服务端一致（`list --json` 的 ram 块）。以前这里写的是
        # 改名前那对 `total` / `available`，页面读到 undefined 就显示「未知」，
        # 而没有任何一条断言会发现 —— 因为没人断言过这几个数字。
        "ram": {"physical_total_mb": 8192.0, "physical_available_mb": 4096.0},
        "background": [],
        "autostart": {
            "total": len(items),
            "enabled": len(items),
            "disabled": 0,
            "memory_mb": 12.5,
            "items": items,
        },
    }
    data.update(extra)
    return data


def check_page_js_escapes_hostile_data() -> None:
    """喂进带标签的数据，看页面 JS 吐出什么 HTML。"""
    hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    payload = _payload(
        [_item(hostile, "run", command=hostile, company=hostile, exe=hostile, flags=["可疑路径"])],
        problems=[hostile],
        background=[{"name": hostile, "mb": 200.0, "count": 1, "path": hostile}],
    )
    blob = _run_page_js(
        payload,
        "draw();console.log(JSON.stringify({t:el('table').innerHTML,"
        "f:el('foot').innerHTML,s:el('stats').innerHTML}));",
    )
    if blob is None:
        print("  · 跳过 JS 渲染检查（没装 node）")
        return
    if "__error__" in blob:
        check("页面 JS 能跑起来", False, blob["__error__"])
        return
    rendered = blob["t"] + blob["f"] + blob["s"]
    check("页面 JS 能跑起来", True)
    check("危险标签没被原样写进 HTML", "<script>" not in rendered and "<img" not in rendered)
    check("危险内容被转义了", "&lt;script&gt;" in rendered and "&lt;img" in rendered)
    check("正常内容仍然渲染", "可疑路径" in rendered and "注册表 · 用户 Run" in rendered)
    # 内存那两个数字必须真的渲染出来。键名对不上时页面会安静地显示「未知」——
    # 不报错、不空白，就是没数字，只有断言具体数值才抓得到。
    shows_memory = "8192.0 MB" in blob["s"] and "4096.0 MB" in blob["s"]
    check(
        "页面把物理内存渲染成数字",
        shows_memory,
        ""
        if shows_memory
        else "stats 里没找到 8192.0 / 4096.0 MB：" + re.sub(r"<[^>]+>", " ", blob["s"])[:80],
    )


def check_page_js_filters() -> None:
    """默认只显示「全部常用」，点分类看那一类，搜不到要说清是搜不到。"""
    payload = _payload([_item("run-item", "run"), _item("svc-item", "service")])
    blob = _run_page_js(
        payload,
        "function h(){return el('table').innerHTML}"
        "draw();const all=h();"
        "q='svc';draw();const miss=h();"
        "kind='service';draw();const svc=h();"
        "console.log(JSON.stringify({all:all,miss:miss,svc:svc}));",
    )
    if blob is None:
        print("  · 跳过 JS 筛选检查（没装 node）")
        return
    if "__error__" in blob:
        check("页面 JS 筛选能跑", False, blob["__error__"])
        return
    check("默认视图含启动项、不含服务", "run-item" in blob["all"] and "svc-item" not in blob["all"])
    check("切到服务分类能看到服务", "svc-item" in blob["svc"])
    plain = re.sub(r"<[^>]+>", "", blob["miss"])
    check("搜不到时说的是搜不到，不是「这一类没有」", "没有匹配" in plain and "全部常用" in plain, plain.strip()[:60])


def check_readme_test_count() -> None:
    """README 徽章上的测试数必须等于真实测试数。

    这个数字是手写的，改测试时很容易忘了同步 —— 以前就写错过（441 vs 439）。
    数字不许拍，只能数。
    """
    import unittest

    loader = unittest.TestLoader()
    total = loader.discover("tests", top_level_dir=".").countTestCases()

    with open("README.md", encoding="utf-8") as handle:
        readme = handle.read()
    badge = re.search(r"tests-(\d+)%20passed", readme)
    body = re.search(r"# (\d+) 个测试", readme)
    if not badge or not body:
        check("README 里有测试数徽章", False, "没找到 tests-N%20passed 或「# N 个测试」")
        return
    check("README 徽章数 = 真实测试数", int(badge.group(1)) == total, f"徽章 {badge.group(1)}，实际 {total}")
    check("README 正文数 = 真实测试数", int(body.group(1)) == total, f"正文 {body.group(1)}，实际 {total}")


def check_python39_syntax() -> None:
    """找 3.9 解析不了的写法 —— 本机只有 3.13，这类错只能靠规则抓。

    典型是 f-string 里用同类引号嵌套：`f"{"a" if c else "b"}"`。3.12 起才允许
    （PEP 701），3.9–3.11 直接 SyntaxError。`ast.parse(feature_version=(3, 9))`
    抓不住这个（试过），所以按模式找。
    """
    pattern = re.compile(r"""f"[^"\n]*\{[^{}\n]*"[^{}\n]*\}""")
    pattern_single = re.compile(r"""f'[^'\n]*\{[^{}\n]*'[^{}\n]*\}""")
    hits: list[str] = []
    for root, _dirs, files in os.walk("pcxray"):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, 1):
                    if line.lstrip().startswith("#"):
                        continue
                    if pattern.search(line) or pattern_single.search(line):
                        hits.append(f"{path}:{lineno}")
    check("没有 3.12 才允许的 f-string 写法", not hits, "、".join(hits[:3]))


def check_autostart_reconciliation() -> None:
    """pcxray 报的条数，必须和系统自己数出来的一致。

    这是「一次看全」这句话的唯一硬证据。不比对的话，漏掉一整块（比如 HKCU 的
    32 位 Run）谁也不会发现 —— 界面上就是少几行，看起来和「这台机器本来就没有」
    一模一样。四个来源各自对账：

    - 注册表：8 个位置逐个读值，按来源标签比名字集合
    - 启动文件夹：`os.listdir` 比名字集合
    - 计划任务：`Get-ScheduledTask` 里有登录/开机触发的条数
    - 服务：`Win32_Service` 里 `StartMode=Auto` 的条数

    非 Windows 上直接跳过（这些来源本来就不存在）。
    """
    import json
    import subprocess
    import sys
    import winreg

    from pcxray import autostart, pwsh, winapi

    if not winapi.IS_WINDOWS:
        print("  · 跳过自启对账（不是 Windows）")
        return

    entries, problems = autostart.scan()
    by_source: dict[str, set[str]] = {}
    for entry in entries:
        by_source.setdefault(entry.source, set()).add(entry.name)

    hives = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}

    # ---- 注册表
    # 「该扫哪些位置」是规格，必须写在这里，不能从 RUN_KEYS 里推出来 —— 否则
    # 表里少一行，循环也就少跑一行，漏掉的那块永远不会被对账发现（试过）。
    canonical = []
    for hive in ("HKCU", "HKLM"):
        for middle in ("Software", r"Software\WOW6432Node"):
            for leaf in ("Run", "RunOnce"):
                canonical.append((hive, middle + "\\Microsoft\\Windows\\CurrentVersion\\" + leaf))
    scanned = {(hive, subkey): (label, kind) for hive, subkey, _s, kind, label in autostart.RUN_KEYS}
    raw_registry_total = 0  # 系统里实际有多少个值（规格侧的基准）
    registry_skipped = 0  # 其中不是字符串、Windows 不会执行的
    raw_by_kind: dict[str, int] = {}
    skipped_by_kind: dict[str, int] = {}
    for hive_name, subkey in canonical:
        found = scanned.get((hive_name, subkey))
        if not found:
            check(f"注册表对账 · {hive_name}\\{subkey}", False, "这个位置没扫")
            continue
        label, kind = found
        try:
            with winreg.OpenKey(hives[hive_name], subkey) as handle:
                raw, index = set(), 0
                while True:
                    try:
                        raw.add(winreg.EnumValue(handle, index)[0])
                    except OSError:
                        break
                    index += 1
        except FileNotFoundError:
            raw = set()
        got = by_source.get(label, set())
        missing = raw - got
        # 非字符串的值本来就不会被列出来，但它会在 problems 里点名 —— 那不算漏
        unexplained = {n for n in missing if not any(f"「{n}」" in p for p in problems)}
        check(
            f"注册表对账 · {label}",
            not unexplained,
            "漏了：" + "、".join(sorted(unexplained)[:3]) if unexplained else "",
        )
        # 记下系统里的真实值个数：后面 --include 的条数要用它当基准，
        # 不能用扫描结果自己算 —— 扫描漏了什么，两边会一起少（试过）。
        raw_registry_total += len(raw)
        registry_skipped += len(missing) - len(unexplained)
        raw_by_kind[kind] = raw_by_kind.get(kind, 0) + len(raw)
        skipped_by_kind[kind] = skipped_by_kind.get(kind, 0) + len(missing) - len(unexplained)

    # ---- 启动文件夹
    raw_folder_total = 0
    for scope, folder, label in autostart._startup_dirs():
        if not os.path.isdir(folder):
            continue
        raw = {n for n in os.listdir(folder) if n.lower() != "desktop.ini"}
        got = by_source.get(label, set())
        check(f"启动文件夹对账 · {label}", raw == got, f"系统有 {len(raw)} 个，报了 {len(got)} 个")
        raw_folder_total += len(raw)

    # ---- --include 不许漏也不许重
    # 这四个来源加起来必须正好等于不加 --include 时的条数。只跑快的两个
    # （注册表、启动文件夹），计划和服务的总数上面已经和系统对过账了。
    # 基准来自系统侧（winreg / os.listdir），不是扫描结果 —— 扫描漏了什么，
    # 拿扫描结果当基准的话两边会一起少，检查就永远是绿的。
    expected_total = {
        "registry": raw_registry_total - registry_skipped,
        "folders": raw_folder_total,
    }
    # --kind 的每一类也要对得上：注册表三类拿系统侧的值个数当基准，
    # 文件夹拿 os.listdir 的个数，计划任务和服务上面已经和 CIM 对过账。
    kind_expect = {
        "run": raw_by_kind.get("run", 0) - skipped_by_kind.get("run", 0),
        "run32": raw_by_kind.get("run32", 0) - skipped_by_kind.get("run32", 0),
        "runonce": raw_by_kind.get("runonce", 0) - skipped_by_kind.get("runonce", 0),
        "folder": raw_folder_total,
    }
    for kind, want in kind_expect.items():
        done = subprocess.run(
            [sys.executable, "-m", "pcxray", "list", "--json", "--limit", "0", "--kind", kind],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if done.returncode != 0:
            check(f"--kind {kind} 能跑", False, done.stderr[-160:])
            continue
        try:
            got = json.loads(done.stdout)["autostart"]["total"]
        except (json.JSONDecodeError, KeyError) as exc:
            check(f"--kind {kind} 是合法 JSON", False, str(exc)[:120])
            continue
        check(
            f"--kind {kind} 条数对得上",
            got == want,
            f"报了 {got} 条，系统里这一类有 {want} 条" if got != want else f"{got} 条",
        )

    for include in ("registry", "folders"):
        done = subprocess.run(
            [sys.executable, "-m", "pcxray", "list", "--json", "--limit", "0", "--include", include],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if done.returncode != 0:
            check(f"--include {include} 能跑", False, done.stderr[-160:])
            continue
        try:
            got = json.loads(done.stdout)["autostart"]["total"]
        except (json.JSONDecodeError, KeyError) as exc:
            check(f"--include {include} 是合法 JSON", False, str(exc)[:120])
            continue
        want = expected_total[include]
        check(
            f"--include {include} 条数对得上",
            got == want,
            f"报了 {got} 条，系统里这一类有 {want} 条" if got != want else f"{got} 条",
        )

    # ---- 计划任务（登录/开机触发）
    ok, task_out, task_err = pwsh.run_text(
        "Get-ScheduledTask | Where-Object { $_.Triggers | "
        "Where-Object { $_.CimClass.CimClassName -match 'LogonTrigger|BootTrigger' } } | "
        "Measure-Object | Select-Object -ExpandProperty Count",
        timeout=120,
    )
    if ok and task_out.strip().isdigit():
        want = int(task_out.strip())
        got = sum(1 for e in entries if e.kind == "task")
        check("计划任务对账", want == got, f"系统有 {want} 条，报了 {got} 条" if want != got else "")
    else:
        print(f"  · 跳过计划任务对账（PowerShell 没给出数字：{task_err.strip()[:60]}）")

    # ---- 自动服务
    ok, svc_out, svc_err = pwsh.run_text(
        "(Get-CimInstance Win32_Service | Where-Object { $_.StartMode -eq 'Auto' }).Count",
        timeout=120,
    )
    if ok and svc_out.strip().isdigit():
        want = int(svc_out.strip())
        got = sum(1 for e in entries if e.kind == "service")
        check("自动服务对账", want == got, f"系统有 {want} 个，报了 {got} 个" if want != got else "")
    else:
        print(f"  · 跳过自动服务对账（PowerShell 没给出数字：{svc_err.strip()[:60]}）")


def check_page_js_reports_scan_failure() -> None:
    """扫描失败时，页面必须说人话，而不是停在「正在扫描…」。

    后端以前遇到异常直接掐断连接，浏览器只看到 `TypeError: Failed to fetch`。
    现在后端回 500 + JSON，页面要把那句原因照抄出来。两条路都跑真 JS 验。
    """
    tail = (
        ";setTimeout(()=>{console.log(JSON.stringify({sub:(els.sub||{}).textContent||\"\"}))},10);"
    )
    # 1) 服务器回了 500 + 原因
    body = json.dumps({"ok": False, "error": "扫描时出错了：RuntimeError: 扫描时炸了"}, ensure_ascii=False)
    got = _run_page_js(
        {},
        tail,
        fetch_js="async()=>({ok:false,status:500,json:async()=>(" + body + ")})",
    )
    if got is None:
        print("  · 跳过页面报错测试（没装 node）")
        return
    text = got.get("sub", "")
    check("页面照抄服务器给的原因", "扫描时炸了" in text, f"页面显示的是：{text[:60]}")
    check("页面没有停在「正在扫描」", "正在扫描" not in text, f"页面显示的是：{text[:60]}")

    # 2) 后端直接断开（网络层失败）
    got = _run_page_js(
        {},
        tail,
        fetch_js="async()=>{throw new TypeError('Failed to fetch')}",
    )
    text = got.get("sub", "")
    check(
        "网络层失败也说人话",
        "扫描失败" in text and "连不上本机的服务" in text and "Failed to fetch" not in text,
        f"页面显示的是：{text[:60]}",
    )


def check_json_memory_keys() -> None:
    """内存数字的 JSON 契约：同名的量必须同含义，不同含义不许同名。

    以前 `list --json` 的 `ram` 是 `{total, available}`（字节，没说单位），
    `ram --json` 的 `total_mb` 是「所有程序占用之和」—— 两个完全不同的量，
    一个说字节一个说 MB，脚本照着写就是 null 或者差 1e6 倍。
    """
    import json
    import subprocess
    import sys

    def run_json(args: list[str]) -> dict | None:
        done = subprocess.run(
            [sys.executable, "-m", "pcxray"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if done.returncode != 0:
            check(f"{' '.join(args)} 能跑", False, done.stderr[-200:])
            return None
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            check(f"{' '.join(args)} 是合法 JSON", False, str(exc)[:120])
            return None

    payload = run_json(["list", "--json", "--limit", "0"])
    if payload is None:
        return
    block = payload.get("ram") or {}
    check(
        "list --json 的物理内存键名带单位",
        set(block) == {"physical_total_mb", "physical_available_mb"},
        f"实际是 {sorted(block)}",
    )
    total = block.get("physical_total_mb") or 0
    avail = block.get("physical_available_mb") or 0
    check("物理内存是合理的 MB 数", 0 < total < 1_000_000, f"读到 {total}")
    check("可用不超过总量", 0 <= avail <= total, f"可用 {avail} / 总量 {total}")

    ram_payload = run_json(["ram", "--json", "--limit", "0"])
    if ram_payload is None:
        return
    groups = ram_payload.get("groups") or []
    want = ram_payload.get("total_mb")
    got = sum(g.get("mb", 0) for g in groups)
    check(
        "ram --json 的 total_mb = 各程序之和",
        abs(want - got) <= 0.1,
        f"total_mb={want} 求和={round(got, 1)}",
    )
    check(
        "两处的 total 不撞名",
        "total_mb" not in block,
        f"list 的 ram 键：{sorted(block)}",
    )


def check_json_timestamps() -> None:
    """时间戳必须还是 Unix 秒，而且满足 boot <= logon <= now。

    `boot` / `logon` 是裸数字，没有单位也没说纪元。如果有人把它改成 Windows
    FILETIME（1.3e17）或者毫秒（1.7e12），数字看着还在，脚本里 `datetime.
    fromtimestamp()` 直接炸，或者算出 1970 年。这条检查把不变量钉住。
    """
    import json
    import subprocess
    import sys
    import time

    done = subprocess.run(
        [sys.executable, "-m", "pcxray", "list", "--json", "--limit", "0"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if done.returncode != 0:
        check("时间戳检查能跑", False, done.stderr[-200:])
        return
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        check("时间戳检查能解析 JSON", False, str(exc)[:120])
        return
    now = time.time()
    floor = 946684800.0  # 2000-01-01，比这还早说明单位或者纪元不对
    boot = payload.get("boot")
    logon = payload.get("logon")
    for name, value in (("boot", boot), ("logon", logon)):
        check(
            f"{name} 是 Unix 秒",
            isinstance(value, (int, float)) and floor <= value <= now + 60,
            f"读到 {value!r}",
        )
    if isinstance(boot, (int, float)) and isinstance(logon, (int, float)):
        check("开机不晚于登录", boot <= logon + 1, f"boot={boot} logon={logon}")
    generated = payload.get("generated") or ""
    ok = False
    try:
        time.strptime(generated, "%Y-%m-%d %H:%M:%S")
        ok = True
    except ValueError:
        ok = False
    check("generated 是可解析的本地时间", ok, f"读到 {generated!r}")


def check_move_mode_round_trip() -> None:
    r"""`--mode move` 是唯一能当场验证「真的不会启动了」的关法，得端到端跑一遍。

    写一个探针值到 `HKCU\...\WOW6432Node\...\Run`（不需要管理员），然后
    `off --mode move` → 值必须出现在 `Run-pcxray-disabled`、`Run` 里没有；
    `on --mode move` → 必须原样搬回 `Run`。全程用临时 HOME，不碰用户真实备份。
    """
    import tempfile
    import winreg

    from pcxray import winapi

    if not winapi.IS_WINDOWS:
        print("  · 跳过 --mode move 往返（不是 Windows）")
        return

    name = "pcxray_selftest_move"
    base = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
    disabled = base + "-pcxray-disabled"

    def read(subkey: str):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as handle:
                return winreg.QueryValueEx(handle, name)[0]
        except FileNotFoundError:
            return None

    def run_cli(args: list[str], home: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["USERPROFILE"] = home
        env["HOME"] = home
        env["PYTHONIOENCODING"] = "utf-8"
        done = subprocess.run(
            [sys.executable, "-m", "pcxray"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=300,
        )
        return done.returncode, done.stdout + done.stderr

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base) as handle:
        winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, r"C:\Windows\SysWOW64\notepad.exe")
    try:
        with tempfile.TemporaryDirectory() as home:
            code, out = run_cli(["off", name, "--mode", "move"], home)
            check("挪走：命令成功", code == 0, out.strip()[-120:] if code else "")
            still_there = read(base)
            check("挪走：值离开了 Run", still_there is None, "" if still_there is None else repr(still_there))
            moved = read(disabled)
            want = r"C:\Windows\SysWOW64\notepad.exe"
            check("挪走：值到了 Run-pcxray-disabled", moved == want, "" if moved == want else repr(moved))
            code, out = run_cli(["on", name, "--mode", "move"], home)
            check("挪回：命令成功", code == 0, out.strip()[-120:] if code else "")
            back = read(base)
            check("挪回：值回到了 Run", back == want, "" if back == want else repr(back))
            left = read(disabled)
            check("挪回：Run-pcxray-disabled 里没留下副本", left is None, "" if left is None else repr(left))
    finally:
        for subkey in (base, disabled):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as handle:
                    winreg.DeleteValue(handle, name)
            except FileNotFoundError:
                pass


def check_no_syntax_warnings() -> None:
    r"""所有模块都不许有 SyntaxWarning。

    Python 3.12 起，字符串里写 `HKCU\StartupFolder` 这种「无效转义」会在
    **编译时**打一行 SyntaxWarning 到 stderr。3.9-3.11 只是静默的
    DeprecationWarning，所以本地跑得好好的，用户机器上第一次运行就多出一行
    莫名其妙的东西 —— 更糟的是任何「stderr 必须为空」的断言会挂。这条检查把
    每个文件都编译一遍，把警告当错误。
    """
    import py_compile
    import tempfile
    import warnings

    paths = sorted(glob.glob("pcxray/*.py")) + sorted(glob.glob("scripts/*.py"))
    paths += sorted(glob.glob("tests/*.py"))
    bad: list[str] = []
    for path in paths:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                py_compile.compile(
                    path,
                    cfile=os.path.join(tempfile.gettempdir(), "pcxray_warn_probe.pyc"),
                    doraise=True,
                )
            except py_compile.PyCompileError as exc:
                bad.append(f"{path} 编译失败：{str(exc).splitlines()[0][:80]}")
                continue
        for item in caught:
            bad.append(f"{path}：{item.category.__name__}: {item.message}")
    check("没有 SyntaxWarning", not bad, "；".join(bad[:3]))


def check_page_reads_only_real_fields() -> None:
    """页面读的每个字段，服务端必须真的给 —— 一个字段一个字段对。

    这是上一类 bug 的通解：`list --json` 的 `ram` 改了键名，页面还读老名字，
    读到 undefined 就安静地显示「未知」，不报错、不空白、测试全绿。靠人记得
    同步是靠不住的，这里直接从页面源码里把 `DATA.x.y` 抠出来，逐个到真实
    扫描结果里找。
    """
    import json
    import re as _re
    import subprocess
    import sys

    from pcxray import web

    match = _re.search(r"<script>(.*?)</script>", web._PAGE.replace("__TOKEN__", "t"), _re.S)
    if not match:
        check("页面里有脚本", False, "没找到 <script>")
        return
    js = match.group(1)
    done = subprocess.run(
        [sys.executable, "-m", "pcxray", "list", "--json", "--limit", "0"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if done.returncode != 0:
        check("字段契约检查能跑", False, done.stderr[-160:])
        return
    payload = json.loads(done.stdout)

    def walk(obj, path: str):
        node = obj
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None, False
            node = node[part]
        return node, True

    # 尾段可能是个方法/属性（`background.slice`、`problems.length`）。只有**明确
    # 认识**的这些才允许退到上一层 —— 否则「父对象在」就算过，等于这条检查永远
    # 绿（试过：把服务端的 ram 键改名，它照样说没问题）。
    methods = {
        "slice", "join", "length", "map", "filter", "forEach", "find", "some",
        "every", "indexOf", "toString", "toFixed", "includes", "split", "trim",
        "toLowerCase", "concat", "reverse", "sort", "push", "keys", "values",
    }
    missing: list[str] = []
    for path in sorted(set(_re.findall(r"DATA\.([A-Za-z_][A-Za-z0-9_.]*)", js))):
        if walk(payload, path)[1]:
            continue
        parts = path.split(".")
        if parts[-1] in methods and len(parts) > 1 and walk(payload, ".".join(parts[:-1]))[1]:
            continue
        missing.append(path)
    check(
        "页面读的 DATA 字段服务端都给",
        not missing,
        "服务端没有：" + "、".join(missing[:5]) if missing else "",
    )

    # 条目字段：页面用 `i.xxx`，服务端每条自启项必须真有这些键
    items = payload.get("autostart", {}).get("items") or []
    if items:
        item_keys = set(items[0])
        wanted = set(_re.findall(r"\bi\.([A-Za-z_][A-Za-z0-9_]*)", js))
        gone = sorted(wanted - item_keys)
        check(
            "页面读的条目字段服务端都给",
            not gone,
            "条目里没有：" + "、".join(gone[:5]) if gone else "",
        )
    else:
        print("  · 跳过条目字段检查（这台机器没扫到自启项）")

    # 后台程序字段：页面用 `p.xxx`
    background = payload.get("background") or []
    if background:
        bg_keys = set(background[0])
        wanted = set(_re.findall(r"\bp\.([A-Za-z_][A-Za-z0-9_]*)", js))
        gone = sorted(wanted - bg_keys)
        check(
            "页面读的后台字段服务端都给",
            not gone,
            "后台数据里没有：" + "、".join(gone[:5]) if gone else "",
        )
    else:
        print("  · 跳过后台字段检查（这台机器没有后台程序）")


def check_autostart_memory_is_deduped() -> None:
    r"""`autostart.memory_mb` 必须比「直接把各条加起来」小，前提是真有重复。

    同一个进程可能被两条自启项指向（本机 Edge 就有两条），实现是按 **PID**
    去重的。所以：
      - 有重复指向的 exe 时，`memory_mb` 必须**严格小于**各条直接相加；
      - 没有重复时，两者必须**基本相等**。
    不写成「按 exe 去重算一遍再比」—— 那是照着我猜的口径写检查，实现要是
    按别的方式去重（比如一个 exe 起了两个进程），检查就会冤枉它。
    """
    import json
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "pcxray", "list", "--json", "--limit", "0"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if done.returncode != 0:
        check("自启占用检查能跑", False, done.stderr[-160:])
        return
    payload = json.loads(done.stdout)["autostart"]
    items = payload.get("items") or []
    got = payload.get("memory_mb")
    if got is None:
        check("自启占用字段存在", False, "没有 memory_mb")
        return
    running = [i for i in items if i.get("enabled") and (i.get("running_mb") or 0) > 0]
    naive = round(sum(i["running_mb"] for i in running), 1)
    exes = [(i.get("exe") or "").lower() for i in running]
    has_dupes = len(exes) != len(set(exes))
    if has_dupes:
        smaller = got < naive - 0.5
        check(
            "有重复指向时，自启占用比直接相加小",
            smaller,
            "" if smaller else f"memory_mb={got}，直接相加={naive} —— 去重没生效？",
        )
    else:
        same = abs(got - naive) <= 0.5
        check(
            "没有重复指向时，自启占用等于直接相加",
            same,
            "" if same else f"memory_mb={got}，直接相加={naive}",
        )
    check("自启占用不为负", got >= 0, "" if got >= 0 else f"读到 {got}")


def _registry_snapshot() -> dict:
    """把 8 个 Run 位置和 3 个 StartupApproved 栏位全抄一遍。

    自检要在真机器上写注册表（建探针、挪走、再删掉），所以必须有一个办法
    证明「跑完之后和跑之前一模一样」—— 这个函数就是那把尺子。
    """
    import winreg

    from pcxray import autostart

    state: dict[str, object] = {}
    for hive_name, subkey, _scope, _kind, _label in autostart.RUN_KEYS:
        hive = winreg.HKEY_CURRENT_USER if hive_name == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        try:
            with winreg.OpenKey(hive, subkey) as handle:
                index = 0
                while True:
                    try:
                        key_name, value, _kind = winreg.EnumValue(handle, index)
                    except OSError:
                        break
                    state[f"{hive_name}\\{subkey}\\{key_name}"] = value
                    index += 1
        except FileNotFoundError:
            pass
    approved = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
    for column in ("Run", "Run32", "StartupFolder"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved + "\\" + column) as handle:
                index = 0
                while True:
                    try:
                        key_name, value, _kind = winreg.EnumValue(handle, index)
                    except OSError:
                        break
                    state[f"approved\\{column}\\{key_name}"] = value
                    index += 1
        except FileNotFoundError:
            pass
    return state


def check_dry_run_writes_nothing() -> None:
    r"""`--dry-run` 必须一个字节都不改。

    这是整个工具的安全承诺：没加 `--dry-run` 才动手，加了就只说不做。以前
    只有手工试过一次，没有任何自动检查 —— 万一哪天在写注册表之前把 dry_run
    判断漏掉一处，用户的机器就被改了，而所有测试还是绿的。
    """
    import tempfile
    import winreg

    from pcxray import winapi

    if not winapi.IS_WINDOWS:
        print("  · 跳过 --dry-run 检查（不是 Windows）")
        return

    name = "pcxray_selftest_noop"
    run_key = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
    approved = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

    def snapshot() -> dict:
        return _registry_snapshot()


    def run_cli(args: list[str], home: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["USERPROFILE"] = home
        env["HOME"] = home
        env["PYTHONIOENCODING"] = "utf-8"
        done = subprocess.run(
            [sys.executable, "-m", "pcxray"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=300,
        )
        return done.returncode, done.stdout + done.stderr

    # 上一次要是被强杀留下残骸，这一轮会莫名其妙地失败 —— 先清一遍。
    for subkey in (run_key, run_key + "-pcxray-disabled"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, name)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_key) as handle:
        winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, r"C:\Windows\SysWOW64\notepad.exe")
    try:
        before = snapshot()
        with tempfile.TemporaryDirectory() as home:
            # 探针名字里故意不带 dry —— 上一版叫 pcxray_selftest_dryrun，于是
            # 「输出里有 dry」这条断言被名字本身满足了，等于没查（试过）。
            said: list[tuple[str, str]] = []
            for args in (
                ["off", name, "--dry-run"],
                ["off", name, "--dry-run", "--mode", "move"],
                ["on", name, "--dry-run"],
            ):
                code, out = run_cli(args, home)
                said.append((" ".join(args), out))
                check(
                    f"{' '.join(args)} 退出码是 0",
                    code == 0,
                    "" if code == 0 else out.strip()[-120:],
                )
            after = snapshot()
            diff = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            check(
                "演练之后注册表一个字节都没变",
                not diff,
                "" if not diff else "变了：" + "、".join(diff[:3]),
            )
            backups = os.path.join(home, ".pcxray", "backup")
            made = os.listdir(backups) if os.path.isdir(backups) else []
            check("演练之后没有留下备份文件", not made, "" if not made else f"多出 {len(made)} 份：{made[:2]}")
            # 每条演练的输出都得让人看出来「什么也没做」，不能安安静静像做了一样
            silent = [
                label
                for label, text in said
                if not any(word in text for word in ("演练", "不会", "没动", "跳过"))
            ]
            check(
                "每条演练都说明自己没动手",
                not silent,
                "" if not silent else "这些没说：" + "、".join(silent[:3]),
            )
    finally:
        # 清理必须覆盖「被挪走的那个键」和三个开关栏位 —— 反证时 dry_run 被短路，
        # 值真被挪进了 Run-pcxray-disabled，而清理只删了 Run，探针就留在那儿，
        # 后面每一轮对账都多一条（试过）。
        for subkey in (run_key, run_key + "-pcxray-disabled"):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as handle:
                    winreg.DeleteValue(handle, name)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for column in ("Run", "Run32", "StartupFolder"):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, approved + "\\" + column, 0, winreg.KEY_SET_VALUE
                ) as handle:
                    winreg.DeleteValue(handle, name)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def check_readme_options_exist() -> None:
    """README 里写到的每个 `--选项`，命令行必须真的认。

    文档最容易被改坏的地方：代码里删了一个选项，README 还留着。用户照着
    复制粘贴，得到一句 `unrecognized arguments` —— 而所有测试都是绿的，
    因为测试不读 README。
    """
    import re as _re

    from pcxray import cli

    parser = cli.build_parser()
    known: set[str] = set()

    def collect(target) -> None:
        for action in target._actions:
            known.update(action.option_strings)
            if action.__class__.__name__ == "_SubParsersAction":
                for sub in action.choices.values():
                    collect(sub)

    collect(parser)
    # README 里有一行在讲「别的软件怎么隐藏窗口」，举了别人的参数，不是我们的
    foreign = {"--silent", "--no-startup-window"}
    text = open("README.md", encoding="utf-8").read()
    mentioned = set(_re.findall(r"--[a-z][a-z0-9-]*", text)) - foreign
    gone = sorted(mentioned - known)
    check(
        "README 里的选项命令行都认",
        not gone,
        "" if not gone else "命令行没有：" + "、".join(gone[:5]),
    )

    # 子命令也要对：README 里 `pcxray xxx` 的 xxx 必须真的是个子命令
    subs: set[str] = set()
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            subs.update(action.choices)
    # 必须允许数字：写成 `[a-z]+` 时，`pcxray prune2` 会被截成 `prune`，
    # 正好是个合法子命令，检查就漏了（试过）。
    used = set(_re.findall(r"pcxray ([a-z][a-z0-9]*)", text))
    missing = sorted(used - subs)
    check(
        "README 里的子命令命令行都认",
        not missing,
        "" if not missing else "命令行没有：" + "、".join(missing[:5]),
    )


def check_no_third_party_imports() -> None:
    """`pcxray/` 里不许 import 任何第三方库 —— README 的「零依赖」徽章是承诺。

    这个徽章是给人看的卖点：装上就能跑，不用 pip install。要是哪天顺手
    `import requests`，本地跑得好好的（机器上装了），用户那边直接 ImportError。
    """
    import ast
    import importlib.util

    stdlib = getattr(sys, "stdlib_module_names", None)
    # 只有 Windows 才有的标准库：Linux/macOS 上 find_spec 找不到，但它们是标准库
    platform_stdlib = {"winreg", "msvcrt", "winsound", "_winapi", "nt", "win32api"}
    modules: set[str] = set()
    for path in sorted(glob.glob("pcxray/*.py")):
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    bad: list[str] = []
    for name in sorted(modules):
        if name == "__future__" or name in platform_stdlib:
            continue
        if stdlib is not None and name in stdlib:
            continue
        spec = importlib.util.find_spec(name)
        if spec is None:
            bad.append(f"{name}（找不到）")
            continue
        origin = spec.origin or ""
        if "site-packages" in origin or "dist-packages" in origin:
            bad.append(f"{name}（第三方）")
    check(
        "pcxray 只用标准库",
        not bad,
        "" if not bad else "这些不是标准库：" + "、".join(bad[:5]),
    )


def check_no_newer_stdlib_apis() -> None:
    """不许用 Python 3.10+ 才有的库函数 —— 徽章写着 3.9 起。

    `ast.parse(feature_version=(3,9))` 只能抓语法（比如 `match`），抓不到
    「语法没问题但这个函数 3.10 才有」。本机只有 3.13，跑测试永远是绿的。
    """
    newer = {
        "zip(strict=)": r"zip\([^)]*strict\s*=",
        "itertools.pairwise": r"\bitertools\.pairwise\b",
        "int.bit_count()": r"\.bit_count\(",
        "datetime.UTC": r"\bdatetime\.UTC\b",
        "tomllib": r"\btomllib\b",
        "typing.Self": r"\btyping\.Self\b",
        "ExceptionGroup": r"\bExceptionGroup\b",
        "asyncio.timeout": r"\basyncio\.timeout\b",
        "dataclasses kw_only": r"kw_only\s*=",
        "isinstance 用 |": r"isinstance\([^)]*\|",
    }
    found: list[str] = []
    for path in sorted(glob.glob("pcxray/*.py")):
        text = open(path, encoding="utf-8").read()
        for label, pattern in newer.items():
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                found.append(f"{label} @ {path}:{line}")
    check(
        "没用到 3.10+ 才有的函数",
        not found,
        "" if not found else "、".join(found[:4]),
    )


def check_served_page_is_the_template_filled_in() -> None:
    r"""真起一次服务，把页面取回来 —— 模板替换对不对，只有这一步能证明。

    `check_page_markup` 是**自己手工**把 `__TOKEN__` 替换掉再检查结构的。要是
    `_send` 里漏替换一个占位符，它照样绿，而用户打开浏览器看到的是一段
    `__TOKEN__`，点任何按钮都 403。所以这里必须走真正的 HTTP。
    """
    import json as _json
    import re as _re
    import threading as _threading
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    from pcxray import web as _web

    server = _web.make_server(0)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, data=None, headers=None):
        request = _urlreq.Request(base + path, data=data, headers=headers or {})
        try:
            with _urlreq.urlopen(request, timeout=60) as response:
                return response.status, response.read().decode("utf-8")
        except _urlerr.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    try:
        status, page = call("/")
        check("取回来的页面状态码是 200", status == 200, f"读到 {status}")
        leftover = sorted(set(_re.findall(r"__[A-Z][A-Z_]{2,}__", page)))
        check("页面里没有没替换的占位符", not leftover, f"还剩 {leftover}")

        match = _re.search(r'const TOKEN="([^"]+)"', page)
        check("页面里带着令牌", bool(match), "" if match else "页面里找不到 const TOKEN=")
        token = match.group(1) if match else ""

        # 用页面里那个令牌真发一次请求：令牌对不对，只有服务器说了算。
        # 用一个不存在的 key —— 鉴权通过才会走到「找不到这些条目」（400），
        # 鉴权没过是 403。这样既验了令牌，又什么都没改。
        body = _json.dumps({"keys": ["__ci_checks_不存在的探针__"]}).encode()
        status, text = call(
            "/api/off",
            body,
            {"Content-Type": "application/json", "X-PCXRAY-Token": token, "Origin": base},
        )
        check("用页面里的令牌能通过鉴权", status == 400, f"读到 {status}：{text[:120]}")
        rejected = "找不到这些条目" in text
        check("不存在的 key 被如实拒绝", rejected, "" if rejected else f"回复不对：{text[:160]}")

        status, body = call("/api/report")
        check("报告接口返回 200", status == 200, f"读到 {status}")
        report = _json.loads(body)
        autostart = report.get("autostart") or {}
        items = autostart.get("items") or []
        check(
            "报告里 total 等于条目数",
            autostart.get("total") == len(items),
            f"total={autostart.get('total')}，条目 {len(items)} 条",
        )
        if autostart.get("enabled") is not None and autostart.get("disabled") is not None:
            adds_up = autostart["enabled"] + autostart["disabled"] == autostart["total"]
            check(
                "报告里启用 + 禁用 等于总数",
                adds_up,
                ""
                if adds_up
                else f"{autostart['enabled']} + {autostart['disabled']} != {autostart['total']}",
            )
        ram = report.get("ram") or {}
        check(
            "报告里可用内存不超过总内存",
            0 <= ram.get("physical_available_mb", 0) <= ram.get("physical_total_mb", 0),
            f"可用 {ram.get('physical_available_mb')} / 总 {ram.get('physical_total_mb')}",
        )
        check(
            "报告里进程数不少于后台分组条数",
            (report.get("process_count") or 0) >= len(report.get("background") or []),
            f"进程 {report.get('process_count')}，后台分组 {len(report.get('background') or [])}",
        )
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    print("端到端自检")
    # 先记下这台机器现在的样子，最后再比一遍
    before_state = _registry_snapshot()
    before_backups = set()
    _backups_dir = os.path.join(os.path.expanduser("~"), ".pcxray", "backup")
    if os.path.isdir(_backups_dir):
        before_backups = set(os.listdir(_backups_dir))
    check_bad_arguments()
    check_json_filter()
    check_web_csrf()
    check_served_page_is_the_template_filled_in()
    check_page_markup()
    check_page_js_escapes_hostile_data()
    check_page_js_filters()
    check_no_handle_leak()
    check_json_contract()
    check_autostart_reconciliation()
    check_page_js_reports_scan_failure()
    check_json_memory_keys()
    check_json_timestamps()
    check_move_mode_round_trip()
    check_no_syntax_warnings()
    check_page_reads_only_real_fields()
    check_autostart_memory_is_deduped()
    check_dry_run_writes_nothing()
    check_readme_test_count()
    check_readme_options_exist()
    check_no_third_party_imports()
    check_python39_syntax()
    check_no_newer_stdlib_apis()
    # 自检自己在真机器上写注册表（建探针、挪走、再删掉）。跑完必须和跑之前
    # 一模一样 —— 否则「自检」本身就成了污染源（真发生过：dry_run 被短路时
    # 探针留在 Run-pcxray-disabled 里，后面每一轮对账都多一条）。
    from pcxray import winapi as _winapi

    if _winapi.IS_WINDOWS:
        after = _registry_snapshot()
        changed = sorted(k for k in set(before_state) | set(after) if before_state.get(k) != after.get(k))
        check(
            "自检没在这台机器上留下任何改动",
            not changed,
            "" if not changed else "这些变了：" + "、".join(changed[:3]),
        )
        backups = os.path.join(os.path.expanduser("~"), ".pcxray", "backup")
        now_files = set(os.listdir(backups)) if os.path.isdir(backups) else set()
        new_files = sorted(now_files - before_backups)
        check(
            "自检没往真实备份目录里写东西",
            not new_files,
            "" if not new_files else f"多出 {len(new_files)} 份：{new_files[:2]}",
        )
    print()
    if FAILED:
        print(f"  {len(FAILED)} 项没通过：{'、'.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
