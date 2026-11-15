"""端到端自检：拿真的命令行和真的 HTTP 服务跑一遍。

单元测试保证逻辑对，这个脚本保证**装完之后命令行的行为**对。CI 里跑它，
本地也能跑：

    python scripts/ci_checks.py

为什么不用一堆 `python -c` 单行命令塞进 workflow：那些东西本地没法复用，
引号还会被 YAML 和 PowerShell 来回吃掉，改一次错一次。
"""

from __future__ import annotations

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


def check_page_js_escapes_hostile_data() -> None:
    """把页面里那段 JS 真跑一遍（用 node），喂进带标签的数据。

    静态检查只能证明「`esc` 函数存在」，证明不了「每个插值都调了它」。这段
    直接把 `draw()` 跑起来看它吐出什么 HTML —— 那才是浏览器会执行的东西。
    没装 node 就明说跳过，不假装验过。
    """
    from pcxray import web

    node = shutil.which("node")
    if not node:
        print("  · 跳过 JS 渲染检查（没装 node）")
        return

    page = web._PAGE.replace("__TOKEN__", "test-token")
    match = re.search(r"<script>(.*?)</script>", page, re.S)
    if not match:
        check("页面里有脚本块", False)
        return
    script = match.group(1)

    hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    payload = {
        "generated": "2026-01-01 00:00:00",
        "process_count": 1,
        "problems": [hostile],
        "ram": {"total": 8 * 1048576, "available": 4 * 1048576},
        "background": [{"name": hostile, "mb": 200.0, "count": 1, "path": hostile}],
        "autostart": {
            "total": 1,
            "enabled": 1,
            "disabled": 0,
            "memory_mb": 12.5,
            "items": [
                {
                    "key": "k1",
                    "name": hostile,
                    "kind": "run",
                    "command": hostile,
                    "detail": "",
                    "source": "注册表 · 用户 Run",
                    "company": hostile,
                    "enabled": True,
                    "can_disable": True,
                    "needs_force": False,
                    "refuse": "",
                    "flags": ["可疑路径"],
                    "running_mb": 12.5,
                    "exe": hostile,
                }
            ],
        },
    }

    harness = (
        "const els={};"
        "function el(id){if(!els[id])els[id]={innerHTML:'',textContent:'',"
        "classList:{add(){},remove(){}},onclick:null,dataset:{}};return els[id];}"
        "global.document={getElementById:el,querySelectorAll:()=>[]};"
        "global.fetch=async()=>{throw new Error('不该发请求')};"
        "global.confirm=()=>true;\n"
        + script
        + "\nDATA = "
        + json.dumps(payload, ensure_ascii=False)
        + ";draw();console.log(JSON.stringify({t:el('table').innerHTML,"
        "f:el('foot').innerHTML,s:el('stats').innerHTML}));"
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "page.js")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(harness)
        proc = subprocess.run(
            [node, path], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
    if proc.returncode != 0:
        check("页面 JS 能跑起来", False, proc.stderr.strip()[:300])
        return

    blob = json.loads(proc.stdout)
    rendered = blob["t"] + blob["f"] + blob["s"]
    check("页面 JS 能跑起来", True)
    check("危险标签没被原样写进 HTML", "<script>" not in rendered and "<img" not in rendered)
    check("危险内容被转义了", "&lt;script&gt;" in rendered and "&lt;img" in rendered)
    check("正常内容仍然渲染", "可疑路径" in rendered and "注册表 · 用户 Run" in rendered)


def main() -> int:
    print("端到端自检")
    check_bad_arguments()
    check_json_filter()
    check_web_csrf()
    check_page_markup()
    check_page_js_escapes_hostile_data()
    check_no_handle_leak()
    check_json_contract()
    print()
    if FAILED:
        print(f"  {len(FAILED)} 项没通过：{'、'.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
