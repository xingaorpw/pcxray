# pcxray

[![CI](https://github.com/xingaorpw/pcxray/actions/workflows/ci.yml/badge.svg)](https://github.com/xingaorpw/pcxray/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-362%20passed-brightgreen)](https://github.com/xingaorpw/pcxray/tree/main/tests)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![deps](https://img.shields.io/badge/dependencies-0-brightgreen)](https://github.com/xingaorpw/pcxray/blob/main/pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-blue)](https://github.com/xingaorpw/pcxray/blob/main/LICENSE)

查清开机时谁自己启动了、后台谁在占内存，还能一条条关掉并随时还原。

零依赖，只用 Python 标准库。默认只读，不点不改。

```bash
git clone https://github.com/xingaorpw/pcxray.git
cd pcxray
python -m pcxray list
```

## 它解决什么问题

任务管理器有两个地方不够用：

- **启动**那一页只列一部分来源。注册表 `Run`、`RunOnce`、32 位的 `WOW6432Node`、两个启动文件夹、登录触发的计划任务、自动启动的服务 —— 它分散在四个地方，没有一个地方能一次看全。
- **进程**那一页按进程列，Chrome 开 20 个进程就是 20 行。你想知道的是「Chrome 一共吃了多少」，不是每一行。

pcxray 把这两件事合成一张表，并且回答一个任务管理器不回答的问题：**这些自启项现在实际占着多少内存。**

## 安装

需要 Windows 10/11 和 Python 3.9 以上。没有任何第三方依赖。

```bash
pip install -e .
pcxray list
```

或者不安装，直接在仓库目录里跑：

```bash
python -m pcxray list
```

## 用法

### 看自启项

```bash
python -m pcxray list
```

真实输出（这台机器，183 条自启）：

```text
  pcxray · 2026-11-15 02:30:53

  内存 13.9 GB  已用 12.9 GB  可用 1013 MB
  开机 11-11 03:54　登录 11-11 09:30
  进程 378 个　自启 183 条（启用 169）
  自启项现在占着 542 MB（同一个进程只算一次；行里的数字是各自程序的全部内存）
  登录后 90 秒内起来的 51 个进程共 838 MB

  开机自启（注册表 + 启动文件夹）
  状态    名称                                            来源          发行商              备注
  ──────  ──────────────────────────────────────────────  ────────────  ──────────────────  ────────────────────
  启用    360huabao                                       用户 Run      —                   不显窗
  启用    ClashVerge                                      用户 Run      Clash Verge Rev     不显窗 占33 MB
  启用    com.electron                                    用户 Run      上海阅文信息技术…
  启用    ctfmon                                          用户 Run      Microsoft Corpora…  占28 MB
  启用    electron.app.Notion                             用户 Run      Notion Labs, Inc    不显窗
  启用    EpicGamesLauncher                               用户 Run      Epic Games, Inc.    不显窗
  启用    Gemini                                          用户 Run      Google LLC
  启用    GoogleUpdaterTaskUser159.0.8140.0               用户 Run      Google LLC          不显窗
  启用    MicrosoftEdgeAutoLaunch_29EBC4579851B72EE312C…  用户 Run      Microsoft Corpora…  不显窗 占411 MB ×2条
  启用    MicrosoftEdgeAutoLaunch_A96907E98764DBDE45606…  用户 Run      Microsoft Corpora…  不显窗 占411 MB ×2条
  启用    OneDrive                                        用户 Run      Microsoft Corpora…  不显窗 占22 MB
  启用    quark                                           用户 Run      The Quark Authors   不显窗
  启用    QuarkUpdaterTaskUser1.0.0.21                    用户 Run      —                   !文件没了 不显窗
  启用    RtkAudUService                                  所有用户 Run  Realtek Semicondu…  占3 MB ×2条
  启用    想家云(server端).lnk                            用户启动夹    联想(北京)有限公司
  启用    Delete Cached Standalone Update Binary          RunOnce       Microsoft Corpora…
  启用    Delete Cached Update Binary                     RunOnce       Microsoft Corpora…
  启用    Uninstall 26.198.1011.0006                      RunOnce       Microsoft Corpora…
  启用    msedge_cleanup_{56EB18F8-B008-4CBD-B6D2-8C97F…  RunOnce       Microsoft Corpora…
  启用    msedge_cleanup_{C50565E9-CCCF-44B4-BA15-5AC5C…  RunOnce       Microsoft Corpora…
  启用    msedge_cleanup_{F3017226-FE2A-4295-8BDF-00C3A…  RunOnce       Microsoft Corpora…
  已禁用  LVAPRCU                                         所有用户 Run  Lenovo Limited Co…
  已禁用  SecurityHealth                                  所有用户 Run  Microsoft Corpora…  占3 MB

  还有：计划任务 46 条（启用 34）　自动服务 114 条（启用 114）
  　用 pcxray list --kind task / --kind service 看明细

  后台占内存（没有可见窗口，超过 100 MB）
  程序                  进程    内存  启动              路径
  ────────────────────  ────  ──────  ────────────────  ────────────────────────────────────────
  DeepSeek Harness.exe     3  5.2 GB  10:07 启动        C:\Users\admin\AppData\Local\Programs\D…
  WorkBuddy.exe            3  473 MB  19:37 启动        C:\Users\admin\AppData\Local\Programs\W…
  MsMpEng.exe              1  278 MB  启动时间未知      读不到路径
  Grok Bot.exe             2  269 MB  02:05 启动        C:\Users\admin\AppData\Local\Programs\G…
  LenovoPcManager.exe      1  148 MB  07:31 启动        C:\Program Files (x86)\Lenovo\PCManager…
  msedgewebview2.exe       1  111 MB  09:31 随登录启动  C:\Program Files (x86)\Microsoft\EdgeWe…
  GlobalPresenter.exe      1  102 MB  09:32 启动        C:\Program Files\WindowsApps\E046963F.L…

  · 不是管理员：175 个进程读不到路径和启动时间。要完整信息用 pcxray list --elevate
```

几个字段的意思：

| 列 | 含义 |
| --- | --- |
| 来源 | `用户 Run` / `所有用户 Run` / `RunOnce` / `用户启动夹` / `计划任务` / `服务` |
| 发行商 | 读 exe 的版本信息。空白说明这个程序没有签名信息，不等于恶意 |
| 不显窗 | 启动时带 `--silent`、`/background`、`--no-startup-window` 这类参数 |
| !文件没了 | 指向的 exe 已经不在了 —— 装完又删掉的残留，关掉它没有任何损失 |
| 占 X MB | 这一条现在实际拉起来的进程占了多少内存 |
| ×N条 | 有 N 条自启项指向同一个程序。这时每行都显示这个程序的全部内存，所以**行加起来会比顶部合计大** —— 顶部那个数是按进程去重后的真实占用 |

上面那两条 `MicrosoftEdgeAutoLaunch_…` 就是这种情况：同一个 `msedge.exe` 被两条自启项指着，各显示 411 MB，而顶部合计只算一次。这类重复项本身就该关掉一条。

### 看谁在占内存

```bash
python -m pcxray ram
```

```text
  内存 13.9 GB　可用 1022 MB　进程 378 个

  程序                  进程数    占用    提交  窗口  启动
  ────────────────────  ──────  ──────  ──────  ────  ────────────────
  DeepSeek Harness.exe       6  5.4 GB  9.8 GB  有    10:07 启动
  svchost.exe              100  740 MB  779 MB  无    09:30 登录前启动
  Memory Compression         1  736 MB    7 MB  无    启动时间未知
  Grok Bot.exe               9  665 MB  831 MB  有    02:05 启动
  WorkBuddy.exe              8  598 MB  1.4 GB  无    19:37 启动
  msedge.exe                19  411 MB  1.6 GB  有    03:21 启动
  msedgewebview2.exe        13  327 MB  641 MB  无    09:30 随登录启动
  MsMpEng.exe                1  280 MB  496 MB  无    启动时间未知

  「占用」= 工作集，和任务管理器那一列一致
  「提交」= 已申请的内存，含被换到页面文件的部分，可能比占用大好几倍
  · 不是管理员：部分系统进程的路径和启动时间读不到，内存数字不受影响
```

**「提交」比「占用」大好几倍是正常的**，不是 bug。占用是真正在物理内存里的部分，提交是程序已经申请的全部内存（含被换到页面文件的部分）。

### 关掉一条自启

```bash
# 先看它要做什么，不改任何东西
python -m pcxray off pcxray_demo --dry-run

# 真的关掉
python -m pcxray off pcxray_demo
```

真实输出（拿一条自己建的测试项 `pcxray_demo` 演示，跑完就删掉了）：

```text
  ✓ pcxray_demo　（演练）将要禁用（写 Run 开关）
```

```text
  ✓ pcxray_demo　已写入开关标记


  备份已写入：C:\Users\admin\.pcxray\backup\20261115-023116.json
  反悔就运行：pcxray restore "C:\Users\admin\.pcxray\backup\20261115-023116.json"

  重启后生效。想立刻确认这一条还会不会启动，用 --mode move 再关一次。
```

名字写得太模糊会**拒绝执行**，不会替你猜：

```text
  ✗ MicrosoftEdge
      匹配到 2 条，请写更完整的名字：MicrosoftEdgeAutoLaunch_29EBC4579851B72EE312C449CF839B1A
      （注册表 · 当前用户 Run）、MicrosoftEdgeAutoLaunch_A96907E98764DBDE45606310F62241CD（注
      册表 · 当前用户 Run）
```

### 两种关法

| 方式 | 命令 | 原理 | 什么时候用 |
| --- | --- | --- | --- |
| 开关标记（默认） | `off 名字` | 写 `Explorer\StartupApproved`，等于在任务管理器里点「禁用」 | 想保持列表干净、随时能改回来 |
| 改名 | `off 名字 --mode move` | 把 `Run` 的值改名成 `~pcxray~原名` | 需要**当场**确认它不会再启动 |

默认方式不删你的任何数据，只写一个状态标记，重启后生效。`--mode move` 立刻生效，`pcxray on 名字 --mode move` 改回来。

### 恢复

```bash
python -m pcxray backups          # 看有哪些备份
python -m pcxray restore          # 用最近一份还原
python -m pcxray restore "C:\Users\admin\.pcxray\backup\20261114-224350.json"
```

每次改动前都会写一份备份，**备份写不进去就不改**。

### 网页界面

```bash
python -m pcxray web
```

浏览器打开 `http://127.0.0.1:<随机端口>/`，能筛、能搜、能一条条点。只绑 `127.0.0.1`，不对外网开放。

网页里**只能关和开，不能还原**——每次操作后它会打出备份文件的完整路径，还原用命令行那条 `pcxray restore`。这样设计是因为还原会动系统，留在命令行里你至少能看到它写了什么。

### 其它

```bash
python -m pcxray list --kind task       # 只看计划任务
python -m pcxray list --kind service    # 只看服务
python -m pcxray list --json            # 给脚本用（--kind / --limit 一样生效）
python -m pcxray list --elevate         # 以管理员身份重开（能多看到一批进程信息）
python -m pcxray backups --all          # 列出全部备份（默认只给最近 10 份）
python -m pcxray off 名字 --force       # 允许改服务（默认只给建议）
```

`--kind` 只认 `run` / `run32` / `runonce` / `folder` / `task` / `service`，`--include` 只认 `registry` / `folders` / `tasks` / `services`。**写错会当场报错退出**，不会静默给你一份「这一类没有条目」的空报告。

## 它不会做什么

- **不删你的东西。** 关自启写的是 Windows 自己的开关，不删注册表值。
- **不动系统组件。** 名字在保护名单里、或者 exe 落在 `Windows\System32` 下的服务，一律拒绝，`--force` 也不行。
- **不替你猜。** 名字匹配到多条就报错，列出候选让你写清楚。
- **不偷偷提权。** `--elevate` 会弹 UAC，你点了才会提权。
- **不联网。** 一行网络代码都没有。

## 已知限制

这些都是实测出来的，不是猜的：

1. **`off` 的禁用标记没有经过重启验证。** 写的是 `StartupApproved` 里 12 字节的标记：启用写 `02 00 00 00` + 8 个零，禁用写 `03 00 00 00` + 8 字节时间戳（和任务管理器点「禁用」时写的形式一致）。本机还能看到别的工具写的 `04 00 00 00` + 8 个零，读取时只认首字节（2 或 6 = 启用，其它 = 禁用），两种都能正确识别。写入后会立刻读回来确认字节一致，但「重启后确实不启动」这一步没法在开发机上验证 —— 重启一次代价太大。想立刻确认生效，用 `--mode move`，那条路径当场可验证。
2. **非管理员看不到全部进程的路径。** 实测 372 个进程里有 179 个拿不到路径和启动时间（受保护进程）。**内存数字不受影响**，那是另一条不需要句柄的路径拿的。要看路径用 `--elevate`。
3. **内存数字依赖一个未文档化的系统调用。** `NtQuerySystemInformation(SystemProcessInformation)` 一次就能拿到所有进程的内存，不需要逐个开句柄 —— 这是非管理员也能看到全部内存的唯一办法。因为它没文档，启动时会拿 6 个进程和官方接口 `GetProcessMemoryInfo` 交叉比对，偏差超过 20% 就整体退回逐个读（覆盖少一半但数字都是真的），并在界面上写明。
4. **服务的「关掉」是改成手动启动，不会立刻停止。** 重启后才生效。而且非管理员根本改不了服务，会直接拒绝。
5. **读不到可执行文件路径的服务一律按系统组件处理。** 非管理员下确实有一批服务的 `PathName` 读不到（CI 的 Windows runner 上就有）。这种情况下判断不了关掉会怎样，所以不给开关 —— 备注列会写「读不到可执行文件路径，按系统组件处理」。这是有意的保守。
6. **启动文件夹里解不出来的快捷方式只显示文件名。** 目标路径靠 PowerShell 的 `WScript.Shell` 解，某些 .lnk 会失败，这时会明确写「解不出来」，不会瞎猜一个路径。
7. **计划任务和服务靠 PowerShell 读。** 读不到（PowerShell 被策略禁掉、超时）会明确显示「读不到」，不会当成「没有」。
8. **没有数字签名校验。** 「发行商」只是读 exe 里自带的版本信息，任何人都能写。它只能帮你判断「这个程序有没有自报家门」，不能证明安全性。
9. **同名进程合并显示。** 两个不同目录下同名的 exe 会被算成一行。实际很少见，但确实会。
10. **不能看到自启项「上次启动用了多久」。** 那需要读事件日志，没做。
11. **控制台代码页会被改成 UTF-8。** 不切的话英文 Windows 上打中文会直接 `UnicodeEncodeError` 崩溃（CI 上真崩过一次）。改了之后中文能正常显示，代价是同一个窗口里后续别的命令也按 UTF-8 解释。
12. **同一个程序被多条自启项指向时，表格里会出现多行。** 每行都显示这个程序的全部内存（备注列标 `×N条`），顶部合计按进程去重。这不是算错了，是「一个程序、多个入口」的事实 —— 本机就有两条 Edge 自启项指向同一个 `msedge.exe`，各显示 411 MB，而真实占用是 411 MB 不是 822 MB。
13. **计划任务和服务改完会立刻读回状态确认。** 读回来对不上就报失败。但如果任务被组策略锁定、或者服务需要管理员权限，改不动就是改不动，会明确报错而不是假装成功。
14. **`backups` 默认只列最近 10 份。** 攒到几百份时一次全打印既没法看也没法用，`--all` 才给全部。

## 开发

```bash
python -m unittest discover -s tests -t .     # 362 个测试
```

测试分两类：不依赖 Windows 的逻辑（命令行解析、开关状态解码、内存分组、表格排版、网页接口）在 Linux 上也能跑；真正读写注册表的端到端测试只在 Windows 上跑（`skipUnless`）。CI 两边都跑。

## 许可

MIT
