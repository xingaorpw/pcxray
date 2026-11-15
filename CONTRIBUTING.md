# 贡献

## 硬约定

1. **不许引第三方依赖。** 只用 Python 标准库。这条不是洁癖 —— 这个工具要能在一个 `python -m pcxray` 里跑起来，不装任何东西。
2. **不许为了过测试改断言。** 测试挂了先怀疑代码。如果确认是测试写错了，在提交信息里说清为什么。
3. **不许在非 Windows 上静默跳过整个模块。** 纯逻辑（解析、排版、判断）必须能在 Linux 上测；只有真正碰注册表/ctypes 的部分才允许 `skipUnless(sys.platform == "win32")`。
4. **改动系统的地方必须有备份和还原路径。** `pcxray/actions.py` 是唯一会写系统的文件，新增写操作必须同时加：备份记录、`restore` 分支、以及一条真的跑一遍的测试。
5. **界面上说的「读不到」必须真的是读不到。** 不允许把「读取失败」变成空列表 —— 空列表和「真的没有」在界面上长得一模一样。
6. **数字必须有来源。** README 里的输出、注释里的实测值，都必须是真跑出来的。改动了行为就重新抓一遍输出。

## 开发流程

```bash
git clone https://github.com/xingaorpw/pcxray.git
cd pcxray
python -m unittest discover -s tests -t .
python -m pcxray list
```

## 加一个新的自启来源

1. 在 `pcxray/autostart.py` 里写一个 `scan_xxx() -> list[Entry]`。
2. 每个 `Entry` 必须带 `locator`（改的时候要用的定位信息），不要只给展示用的 `detail`。
3. 在 `pcxray/actions.py` 的 `_plan()` 里加上这个 kind 的改法，并在 `restore()` 里加上对应的还原分支。
4. 在 `pcxray/safety.py` 的 `refuse_reason()` 里想清楚：这条能不能关？关错会怎样？
5. 加测试。至少要有：解析逻辑的单元测试 + 一条在 Windows 上真跑的端到端测试。

## 写测试时的一个坑

`winapi` / `pwsh` 是在**函数内部**懒导入的（为了在非 Windows 上也能 import 整个包）。
所以 `mock.patch.object(actions, "pwsh", ...)` 会报 `AttributeError: module
'pcxray.actions' has no attribute 'pwsh'` —— 模块级根本没有这个名字。

**patch 被导入的那个模块，不要 patch 导入方：**

```python
from pcxray import actions, pwsh

# 对：懒导入拿到的就是同一个模块对象
with mock.patch.object(pwsh, "run_text", return_value=(True, "Manual", "")):
    actions._set_service("X", False)
```

同一个坑踩了两次（`actions.pwsh`、`safety.winapi`），记在这儿。

## 覆盖率

不是 KPI，是找「哪段逻辑一次都没跑过」的工具：

```bash
python -m pip install coverage      # 只是开发工具，不进项目依赖
python -m coverage run --source=pcxray -m unittest discover -s tests -t .
python -m coverage report --show-missing --skip-covered
```

优先补**判断成功/失败**的那几行（「改完读回来对一遍」「出错时报什么」），
而不是为了数字去点没用的分支。

## 端到端自检

```bash
python scripts/ci_checks.py
```

它跑真命令行、真 HTTP 服务。其中「页面 JS 转义」那一项需要 `node`（在 CI 的
runner 上本来就有）；没装会打印「跳过」，不会假装通过。

## 提交信息

用 `feat:` / `fix:` / `docs:` / `test:` / `refactor:` 前缀，一句话说清改了什么。
