"""本地网页界面：`pcxray web`。

零依赖，只用标准库。绑定 127.0.0.1，起一个随机端口，只服务一个页面和三个接口。

为什么要有网页：自启列表一百多条，终端里翻着看不方便；网页上能一眼扫完、能
按条件筛、能勾选着关。但**改动仍然要一次一次确认** —— 没有「全选然后一键关掉」
这种按钮，那是在鼓励用户把系统搞坏。

**为什么要一个 token。** 只绑 127.0.0.1 不等于安全：你随便打开一个网页，那个
网页就能往 `http://127.0.0.1:<端口>/api/off` 发请求。自启项的 key 是
`sha1(种类|范围|名字)[:12]`，算法公开，攻击者算出 `run|user|OneDrive` 就能点名
关掉它。所以：起服务时生成一个随机 token，写进页面，POST 必须带
`X-PCXRAY-Token`。跨源请求读不到页面里的 token，也带不上自定义头（会被 CORS
预检挡掉）。
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, actions, autostart, report as report_mod, winapi

__all__ = ["serve", "make_server"]

_STATE: dict = {"report": None, "entries": None}
_LOCK = threading.Lock()


def _snapshot(force: bool = False) -> dict:
    """缓存一份报告。网页每次刷新都重扫要 3 秒，太慢。"""
    with _LOCK:
        if _STATE["report"] is None or force:
            entries, problems = autostart.scan()
            rep = report_mod.build(entries=entries)
            rep.problems.extend(problems)
            _STATE["report"] = rep
            _STATE["entries"] = entries
        return _STATE["report"]


_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pcxray · 开机自启与后台内存</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--text:#e6e9ef;--dim:#8b93a7;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--accent:#60a5fa}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
header{padding:20px 28px;border-bottom:1px solid var(--line);display:flex;
align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:600;letter-spacing:.5px}
.sub{color:var(--dim);font-size:13px}
main{padding:20px 28px 60px;max-width:1400px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
gap:12px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat .k{color:var(--dim);font-size:12px;margin-bottom:4px}
.stat .v{font-size:20px;font-weight:600}
.bar{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
button{background:#232833;color:var(--text);border:1px solid var(--line);
border-radius:7px;padding:6px 12px;cursor:pointer;font-size:13px}
button:hover{background:#2b3140}
button.on{background:var(--accent);border-color:var(--accent);color:#0b1020;font-weight:600}
input[type=search]{background:var(--card);border:1px solid var(--line);color:var(--text);
border-radius:7px;padding:6px 10px;font-size:13px;min-width:220px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line);
vertical-align:top;font-size:13px}
th{color:var(--dim);font-weight:500;font-size:12px;position:sticky;top:0;background:#1b1f28}
tr:last-child td{border-bottom:none}
tr:hover td{background:#1c2029}
.name{font-weight:500}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--dim);
word-break:break-all}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
margin-right:4px;border:1px solid var(--line);color:var(--dim)}
.tag.bad{color:var(--bad);border-color:#4a2222;background:#2a1717}
.tag.warn{color:var(--warn);border-color:#4a3a17;background:#2a2417}
.tag.ok{color:var(--ok);border-color:#1c3d2a;background:#14261c}
.state{font-size:12px;padding:1px 7px;border-radius:999px}
.state.on{color:var(--ok);background:#14261c}
.state.off{color:var(--dim);background:#22262f}
.empty{padding:30px;text-align:center;color:var(--dim)}
footer{color:var(--dim);font-size:12px;padding:0 28px 40px;max-width:1400px}
.msg{background:#1d2430;border:1px solid var(--line);border-radius:8px;padding:10px 14px;
margin-bottom:14px;white-space:pre-wrap;font-size:13px}
.msg.err{border-color:#5a2626;background:#241818}
</style></head><body>
<header>
  <h1>pcxray</h1>
  <span class="sub" id="sub">正在扫描…</span>
  <span class="sub" id="ver">v__VERSION__</span>
</header>
<main>
  <div class="stats" id="stats"></div>
  <div id="msg"></div>
  <div class="bar">
    <button data-kind="" class="on">全部常用</button>
    <button data-kind="run">注册表 Run</button>
    <button data-kind="folder">启动文件夹</button>
    <button data-kind="task">计划任务</button>
    <button data-kind="service">服务</button>
    <input type="search" id="q" placeholder="按名字或路径筛…">
    <button id="refresh">重新扫描</button>
  </div>
  <div id="table"></div>
</main>
<footer id="foot"></footer>
<script>
let DATA=null, kind="", q="";
// 服务端起服务时随机生成，写进这个页面。没有它改不了任何东西 ——
// 别的网页读不到它，所以也没法冒名替你关自启项。
const TOKEN="__TOKEN__";
const QUICK=["run","run32","runonce","folder"];
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const mb=v=>v>=1024?(v/1024).toFixed(1)+" GB":(v>=1?v.toFixed(0)+" MB":(v*1024).toFixed(0)+" KB");
function stat(k,v){return `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`}
function draw(){
  if(!DATA) return;
  const a=DATA.autostart;
  document.getElementById("sub").textContent=
    `扫描于 ${DATA.generated}　进程 ${DATA.process_count} 个　自启 ${a.total} 条`;
  const total=DATA.ram.total, avail=DATA.ram.available;
  document.getElementById("stats").innerHTML=
    stat("物理内存", total?mb(total/1048576):"未知")+
    stat("已用", total?mb((total-avail)/1048576):"—")+
    stat("自启项现在占着", mb(a.memory_mb))+
    stat("启用 / 已禁用", `${a.enabled} / ${a.disabled}`);
  let items=a.items.filter(i=>{
    const okKind = kind ? i.kind===kind : QUICK.includes(i.kind);
    const hay=(i.name+" "+i.exe+" "+(i.company||"")).toLowerCase();
    return okKind && (!q || hay.includes(q));
  });
  if(!items.length){
    document.getElementById("table").innerHTML='<div class="empty">这一类没有条目</div>';
    return;
  }
  const rows=items.map(i=>{
    const flags=(i.flags||[]).map(f=>{
      const cls=f.includes("不存在")||f.includes("可疑")?"bad":(f.includes("不显示")?"warn":"");
      return `<span class="tag ${cls}">${esc(f)}</span>`;
    }).join("");
    // 用 data-* 而不是把 key 拼进 onclick：少一层转义风险，也让 CSP 更干净
    const btn=i.can_disable
      ? `<button data-key="${esc(i.key)}" data-on="${i.enabled?1:0}" data-nf="${i.needs_force?1:0}">`
        +`${i.enabled?(i.needs_force?"改成手动":"关掉"):"恢复"}</button>`
      : `<span class="tag bad" title="${esc(i.refuse)}">不能关</span>`;
    return `<tr>
      <td><span class="state ${i.enabled?"on":"off"}">${i.enabled?"启用":"已禁用"}</span></td>
      <td><div class="name">${esc(i.name)}</div>
          <div class="mono">${esc(i.command||i.detail||"")}</div>
          <div>${flags}</div></td>
      <td>${esc(i.source)}</td>
      <td>${esc(i.company||"—")}</td>
      <td>${i.running_mb?mb(i.running_mb):"—"}</td>
      <td>${btn}</td></tr>`;
  }).join("");
  document.getElementById("table").innerHTML=
    `<table><thead><tr><th>状态</th><th>名称 / 命令行</th><th>来源</th>
     <th>发行商</th><th>现在占</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`;
  document.querySelectorAll("#table button[data-key]").forEach(b=>{
    b.onclick=()=>toggle(b.dataset.key, b.dataset.on==="1", b.dataset.nf==="1");
  });
  const bg=DATA.background.slice(0,10).map(p=>
    `<tr><td>${esc(p.name)}${p.count>1?` <span class="tag">${p.count} 个进程</span>`:""}</td>
     <td>${mb(p.mb)}</td>
     <td class="mono">${esc(p.path||"读不到路径")}</td></tr>`).join("");
  document.getElementById("foot").innerHTML=
    `<h3 style="font-size:14px;margin:26px 0 10px">后台占内存（没有可见窗口，前 10）</h3>`+
    `<table><thead><tr><th>程序</th><th>内存</th><th>路径</th></tr></thead><tbody>${bg}</tbody></table>`+
    (DATA.problems.length?`<p style="color:#fbbf24">读不到的部分：${esc(DATA.problems.join("；"))}</p>`:"");
}
function say(text,err){
  const el=document.getElementById("msg");
  el.innerHTML=text?`<div class="msg ${err?"err":""}">${esc(text)}</div>`:"";
}
async function toggle(key,enabled,needsForce){
  if(needsForce && enabled &&
     !confirm("这是服务。关错会导致开不了机，而且要重启后才会发现。\\n\\n确定把它的启动方式改成「手动」吗？"))
    return;
  const url=enabled?"/api/off":"/api/on";
  say("正在处理…");
  try{
    const r=await fetch(url,{method:"POST",
      headers:{"Content-Type":"application/json","X-PCXRAY-Token":TOKEN},
      body:JSON.stringify({keys:[key],force:!!needsForce})});
    const j=await r.json();
    say((j.results||[]).map(x=>(x.ok?"✓ ":"✗ ")+x.name+"　"+x.message).join("\\n")
        +(j.backup?"\\n\\n备份："+j.backup:""), !j.ok);
  }catch(e){ say("请求失败："+e, true); }
  await load(true);
}
async function load(force){
  try{
    const r=await fetch("/api/report"+(force?"?refresh=1":""));
    DATA=await r.json(); draw();
  }catch(e){ document.getElementById("sub").textContent="扫描失败："+e; }
}
document.querySelectorAll("[data-kind]").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("[data-kind]").forEach(x=>x.classList.remove("on"));
  b.classList.add("on"); kind=b.dataset.kind; draw();
});
document.getElementById("q").oninput=e=>{ q=e.target.value.trim().toLowerCase(); draw(); };
document.getElementById("refresh").onclick=()=>{ say(""); load(true); };
load(false);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "pcxray/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 不要往终端刷访问日志
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 本地页面，禁止任何外部来源嵌入或嗅探
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # 页面里只有内联样式和一段内联脚本，别的一律不许加载 —— 万一哪天
        # 有条自启项的名字带了奇怪的 HTML，也不会变成向外的请求
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _authorised(self) -> tuple[bool, str]:
        """这个 POST 是不是从我们自己的页面发出来的。

        两道门：随机 token（跨源读不到）+ Origin 同源检查（挡掉被别的页面
        冒名顶替的情况）。任何一道不过就拒绝。
        """
        expected = getattr(self.server, "token", "")
        got = self.headers.get("X-PCXRAY-Token") or ""
        if not expected or not secrets.compare_digest(got, expected):
            return False, "缺少或错误的访问令牌，请从 pcxray 打开的页面里操作"
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host") or ""
            if origin != f"http://{host}":
                return False, "来源不被信任"
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return False, "请求必须是 application/json"
        return True, ""

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 65536:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - 标准库约定的方法名
        parsed = urlparse(self.path)
        if parsed.path == "/":
            page = (
                _PAGE.replace("__VERSION__", __version__)
                .replace("__TOKEN__", getattr(self.server, "token", ""))
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/report":
            # 用 parse_qs 而不是字符串包含 —— "refresh=10" 里也有 "refresh=1"
            query = parse_qs(parsed.query or "")
            force = (query.get("refresh") or [""])[0] == "1"
            self._json(200, _snapshot(force=force).as_dict())
            return
        self._json(404, {"ok": False, "error": "没有这个地址"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/off", "/api/on"):
            self._json(404, {"ok": False, "error": "没有这个地址"})
            return
        allowed, why = self._authorised()
        if not allowed:
            self._json(403, {"ok": False, "error": why})
            return
        body = self._read_body()
        keys = body.get("keys") or []
        if not isinstance(keys, list) or not keys:
            self._json(400, {"ok": False, "error": "要指定至少一条"})
            return
        if len(keys) > 20:
            # 一次改一堆是最容易后悔的操作，强制拆开
            self._json(400, {"ok": False, "error": "一次最多改 20 条"})
            return
        turn_on = parsed.path == "/api/on"
        force = bool(body.get("force"))
        # 改状态这一段必须在锁里：不然会出现「改完了，页面还是旧的」——
        # 一次并发的刷新可能正好在写之前扫完，把旧结果写进缓存，而我们的
        # 作废标记在那之前就被覆盖掉了。
        with _LOCK:
            entries = _STATE["entries"] or []
            picked = [e for e in entries if e.key in keys]
            if not picked:
                self._json(400, {"ok": False, "error": "找不到这些条目，可能已经变了，刷新一下"})
                return
            results, backup = (
                actions.enable(picked, force=force) if turn_on else actions.disable(picked, force=force)
            )
            _STATE["report"] = None  # 下次取报告重新扫
        self._json(
            200,
            {
                "ok": all(r.ok for r in results),
                "backup": backup,
                "results": [{"name": r.name, "ok": r.ok, "message": r.message} for r in results],
            },
        )

    def do_PUT(self) -> None:  # noqa: N802
        self._json(405, {"ok": False, "error": "不支持的方法"})

    do_DELETE = do_PUT


class _Server(ThreadingHTTPServer):
    """带一个随机 token 的服务器。

    token 挂在服务器实例上而不是模块全局：起两个服务器（测试里会）各有各的
    token，不会互相串。
    """

    daemon_threads = True
    # Windows 上 SO_REUSEADDR 的语义和 Unix 不同：它允许**别的进程**绑同一个
    # 端口把你的请求接走。这个接口能关自启项，端口被抢走就等于被人替你做决定。
    # 所以 Windows 上必须关掉，冲突时老老实实报错。
    allow_reuse_address = not winapi.IS_WINDOWS

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.token = secrets.token_urlsafe(18)


def make_server(port: int = 0) -> _Server:
    return _Server(("127.0.0.1", port), Handler)


def serve(port: int = 0, open_browser: bool = True) -> int:
    if not winapi.IS_WINDOWS:
        print("pcxray 只能在 Windows 上运行。", file=sys.stderr)
        return 2
    # 先占端口再扫描。反过来的话端口冲突时要白等三秒扫描，然后才告诉你起不来。
    try:
        server = make_server(port)
    except OSError as exc:
        # 失败信息走 stderr：脚本会重定向 stdout，把错误混进正常输出里看不见
        print(f"  端口 {port} 起不来：{exc}", file=sys.stderr)
        print("  换一个端口，或者用 --port 0 让系统随便挑一个（默认就是这样）", file=sys.stderr)
        return 1
    print("  正在扫描，稍等（计划任务和服务要调 PowerShell，约 3 秒）…")
    _snapshot(force=True)
    host, real_port = server.server_address[:2]
    url = f"http://{host}:{real_port}/"
    print("")
    print(f"  界面已就绪：{url}")
    print("  按 Ctrl+C 退出")
    print("")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出。")
    finally:
        server.server_close()
    return 0
