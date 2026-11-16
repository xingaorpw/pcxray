"""假装自己是 Linux，把整套测试跑一遍。

为什么需要它：这套代码只在 Windows 上开发，CI 的 Linux 任务才是第一次真正的
异平台运行。推送之前不知道 Linux 上会红，只能靠推上去看 —— 一轮十几分钟。
这个脚本把「不像 Windows」的三件事都摆好：

  1. `sys.platform` 不是 win32（平台判断走 else 分支）
  2. `import winreg` 直接失败（Linux 上没有这个模块）
  3. `ctypes.WinDLL` 失败（Linux 上没有这些 DLL）

它不能替代真 Linux（路径分隔符、权限模型还是 Windows 的），但能抓住
「漏了平台判断」「悄悄 import 了 winreg」这类最常见的红。
"""
import builtins
import sys

sys.platform = "linux"

# 只挡 winreg：msvcrt 是 Windows 的 stdlib 自己在用（subprocess / getpass），
# 挡掉它会让整个测试收集阶段就崩，跟真 Linux 不是一回事。
_BLOCKED = {"winreg", "_winreg"}
_real_import = builtins.__import__


def _fake_import(name, *args, **kwargs):
    if name.split(".")[0] in _BLOCKED:
        raise ImportError(f"No module named '{name}'")
    return _real_import(name, *args, **kwargs)


builtins.__import__ = _fake_import
for _mod in [m for m in sys.modules if m.split(".")[0] in _BLOCKED]:
    del sys.modules[_mod]

import ctypes  # noqa: E402


def _no_windll(*args, **kwargs):
    raise OSError("这台机器上没有 WinDLL（假装 Linux）")


ctypes.WinDLL = _no_windll

import unittest  # noqa: E402

sys.path.insert(0, ".")
suite = unittest.TestLoader().discover("tests", top_level_dir=".")
result = unittest.TextTestRunner(verbosity=0).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
