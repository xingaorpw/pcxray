# 更新日志

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-11-15

把「能跑」变成「经得起查」。这一版全是真机跑出来的 bug，每一个都有测试钉住。

### 修正

- **`off <计划任务>` / `off <服务>` 每次都报失败，但动作其实成功了。**
  脚本输出 `'ok'` 交给 JSON 解析器，`ok` 不是 JSON，于是返回
  「PowerShell 输出不是合法 JSON：ok」。任务真的被禁用了，用户看到的却是 ✗，
  会以为没生效然后反复重试。`restore` 同样中招。现在改成按退出码判断，并且
  **改完立刻读回状态确认**（`Disabled` / `Ready`、`Manual` / `Automatic`），
  读回来对不上就报失败。新增真机端到端测试：真建一条计划任务、真关掉、
  用 `Get-ScheduledTask` 从外部确认状态、再还原。
- **任务名里有一个撇号，整段 PowerShell 就语法错误。** 改成标准转义
  （`o'brien` → `'o''brien'`），并加了真机测试。不转义时报出来的是
  `TerminatorExpectedAtEndOfString`，跟真实原因毫无关系。
- **网页接口能被任意网站调用。** 只绑 `127.0.0.1` 不等于安全：你打开的
  任何一个网页都能往 `http://127.0.0.1:<端口>/api/off` 发请求，而自启项的 key
  是 `sha1(种类|范围|名字)[:12]`，算法公开，攻击者算出 `run|user|OneDrive`
  就能点名关掉它。现在起服务时生成随机 token 写进页面，POST 必须带
  `X-PCXRAY-Token`，另外校验 `Origin` 同源和 `Content-Type: application/json`。
  同时补上 CSP、`X-Frame-Options: DENY`、`Referrer-Policy`。
- **`--json` 完全忽略 `--kind` 和 `--limit`。** 每种 kind 都吐出全部 183 条，
  脚本按 kind 过滤时拿到的是错数据，而且看起来一切正常。现在过滤真的生效，
  计数跟着重算。
- **头部合计和表格里的行加起来对不上。** 两条 Edge 自启项都指向同一个
  `msedge.exe`，那 411 MB 在两行里各出现一次，顶部合计却只算一次 ——
  用户没法判断该信哪个数。现在行上标 `×N条`，并说明行里的数字是「各自程序
  的全部内存」。
- **`--kind nope` / `--include nope` 静默通过。** 前者输出一份正常报告，
  后者扫出 0 条 —— 用户会以为「这一类没有条目」或「系统很干净」。现在当场
  报错退出，并列出可选值。
- **`--limit -1` 静默少显示最后一行。** 现在拒绝负数。
- **后台占内存列表里同一个程序出多行。** 一个程序开 3 个进程就 3 行同名条目，
  看起来像重复 bug。现在按程序合并，带进程数。
- **`text.pad` 宽度为负时把内容整段吞掉。** 静默丢数据，改成原样返回。
- **`ram --json` 的 `total_mb` 和「各组相加」差 0.6 MB。** 四舍五入累积，
  用户拿计算器一核就发现对不上。现在先各自取一位小数再求和。
- **测试会往用户真实的 `~/.pcxray/backup` 里写文件。** 跑一遍测试留下十几份
  备份。现在测试用临时目录。
- **`backups` 一次打印几百行。** 默认只给最近 10 份，`--all` 才全给。

### 变更（不兼容）

- `list --json` 里 `background[]` 从「每个进程一项」变成「每个程序一项」：
  `{name, count, mb, path, started}`，不再有 `pid`。

### 新增

- `pcxray backups --all`
- `pwsh.run_text()`：改系统的动作按退出码判断成败，不要求 JSON 输出
- `pwsh.ps_quote()`：PowerShell 单引号转义
- 内存数字的自洽性测试：同一程序多条目、启用/禁用混合、JSON 与 Python API 一致

## [0.1.0] - 2026-11-14

第一个能用的版本。四类自启来源、两条内存数据路径、两种关闭方式。

### 新增

- **自启枚举**：注册表 `Run` / `RunOnce`（含 `WOW6432Node` 32 位视图）、
  用户和公共启动文件夹、登录/开机触发的计划任务、`StartMode=Auto` 的服务。
- **开关状态**：读 `Explorer\StartupApproved` 判断每条是启用还是禁用。
  实测编码为 12 字节，`02…` 启用、`04…` 禁用；混在里面的 `REG_SZ` 脏值
  （本机真有一条）按「没有记录」处理。
- **内存全量读取**：走 `NtQuerySystemInformation(SystemProcessInformation)`，
  非管理员也能拿到全部进程的工作集和私有提交。启动时和官方接口
  `GetProcessMemoryInfo` 抽样比对，偏差超过 20% 就整体退回逐个读并提示。
- **进程归属**：按 exe 路径把自启项和正在运行的进程对上，得出「这一条现在
  占多少内存」。`svchost`、`cmd` 这类共用宿主不做归属，避免一个服务显示
  400 MB 的荒唐数字。
- **登录时间基准**：本机实测开机 03:54、登录 09:30，差 5 个半小时。判断
  「随开机自启」用 `explorer.exe` 的启动时间做基准，不用开机时间。
- **两种关闭方式**：默认写 `StartupApproved` 标记（等同任务管理器里点禁用，
  重启生效）；`--mode move` 把注册表值改名（当场可验证）。
- **备份与还原**：每次改动前写 `~/.pcxray/backup/<时间>.json`，备份写不进去
  就不改。`restore` 按记录逐字节还原，包括「原来没有这条记录」的情况。
- **安全判断**：系统组件（名字在保护名单里，或 exe 落在 `Windows\System32`
  下）一律拒绝，`--force` 也不行；服务需要额外 `--force` 确认。
- **风险标记**：exe 不存在、路径在临时/下载目录、命令行带隐藏窗口参数、
  没有发行商信息。
- **网页界面**：`pcxray web`，只绑 `127.0.0.1`，能筛能搜，服务类需要二次确认。
- **中文对齐**：终端表格按**显示宽度**排版，中文字符算两格。

### 说明

- 禁用标记没有经过重启验证（开发机上不方便重启）。写入后会立刻读回来确认
  字节一致，但「重启后确实不启动」这一步留给用户确认。需要当场确认生效的
  场景请用 `--mode move`。

### 修正（发布后 CI 抓到的）

- **Windows 路径比较不能交给 `os.path`。** `normcase` 在 Linux 上是空操作、
  `basename` 不认 `\`，导致纯逻辑测试在 ubuntu 上挂了（大小写不同的同一条
  路径比不相等、`cmd.exe` 没被识别成宿主程序）。改成自己按两种分隔符切。
- **有的服务 `PathName` 里没有目录部分**（只有 `svchost.exe -k netsvcs`），
  按路径判会漏掉。补一条按 exe 名字判断，并把「读不到路径」的服务一并按
  系统组件处理。
- **英文 Windows 上打中文会崩溃。** 控制台代码页是 cp1252 时 `print` 中文
  抛 `UnicodeEncodeError`，整个命令直接断掉。现在启动时把代码页切到 UTF-8，
  并把 stdout 的编码换成 UTF-8 + `errors="replace"`。
- **没加引号、路径带空格的命令行解析错。** `C:\Program Files\V\x.exe -run`
  会被切成 `C:\Program`。改成按第一个 `.exe` 截断。整条命令行被引号包住的
  情况（`"C:\Windows\system32\svchost.exe -k netsvcs"`）一并修好。
- **一条测试用子串筛 svchost，把第三方 `mysvchost.exe` 也算进来了。**
  改成按 exe 名字精确比较，并在断言失败时打印具体条目的命令行。

[0.1.0]: https://github.com/xingaorpw/pcxray/releases/tag/v0.1.0
