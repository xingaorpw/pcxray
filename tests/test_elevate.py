"""提权：拼出来的命令行必须是能跑的。

这条路径没法在测试里真的点 UAC，但「拼出来的参数对不对」完全可以验证 ——
而它恰恰是出错的地方：以前拼出来的子进程命令是 `python.exe list --elevate`，
python 会去找一个叫 `list` 的文件，报 `can't open file 'list'`，新窗口闪一下
就没了，父进程还在说「已以管理员身份重新打开」。
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from pcxray import winapi


@unittest.skipUnless(sys.platform == "win32", "提权只有 Windows 有")
class TestElevateParams(unittest.TestCase):
    def test_keeps_the_module_flag(self):
        params = winapi.elevate_params(["list", "--elevate"])
        self.assertIn("-m pcxray", params)

    def test_drops_the_elevate_flag_itself(self):
        # 子进程已经是管理员了，留着只是让逻辑多绕一圈
        params = winapi.elevate_params(["list", "--elevate", "--limit", "5"])
        self.assertNotIn("--elevate", params)
        self.assertIn("list", params)
        self.assertIn("--limit 5", params)

    def test_only_elevate_still_has_a_command(self):
        self.assertEqual(winapi.elevate_params(["--elevate"]), "-m pcxray")

    def test_empty_args(self):
        self.assertEqual(winapi.elevate_params([]), "-m pcxray")

    def test_name_with_space_and_chinese_is_quoted(self):
        # 带空格的参数必须加引号，否则提权后会被拆成两个参数
        params = winapi.elevate_params(["off", "想家云 server端.lnk"])
        self.assertIn('"', params)
        self.assertIn("想家云 server端.lnk", params)

    def test_the_command_really_runs(self):
        """拼出来的东西必须真能被 python 执行 —— 这是这条路径的全部意义。"""
        params = winapi.elevate_params(["list", "--limit", "1"])
        # params 里已经带了 -m pcxray，直接交给 python 执行
        proc = subprocess.run(
            [sys.executable] + params.replace('"', "").split(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

    def test_the_old_form_really_breaks(self):
        """反证：不带 -m pcxray 的老写法确实跑不起来。

        没有这条，上面几条只证明「现在有 -m」，证明不了「没有 -m 就是坏的」。
        """
        proc = subprocess.run(
            [sys.executable, "list", "--elevate"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("can't open file", proc.stderr)


if __name__ == "__main__":
    unittest.main()
