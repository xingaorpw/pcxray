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
    check("--limit 真的截断", limited["total"] == 3 and len(limited["items"]) == 3)

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


def main() -> int:
    print("端到端自检")
    check_bad_arguments()
    check_json_filter()
    check_web_csrf()
    check_page_markup()
    check_no_handle_leak()
    print()
    if FAILED:
        print(f"  {len(FAILED)} 项没通过：{'、'.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
