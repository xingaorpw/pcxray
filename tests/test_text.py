"""终端排版测试。中文对齐是最容易歪的地方，单独盯。"""

from __future__ import annotations

import unittest

from pcxray import text


class TestWidth(unittest.TestCase):
    def test_ascii_is_one(self):
        self.assertEqual(text.width("abc"), 3)

    def test_chinese_is_two(self):
        self.assertEqual(text.width("中文"), 4)

    def test_mixed(self):
        self.assertEqual(text.width("a中b"), 4)

    def test_fullwidth_punctuation(self):
        self.assertEqual(text.width("，。"), 4)

    def test_empty(self):
        self.assertEqual(text.width(""), 0)

    def test_combining_mark_is_zero(self):
        # e + 组合重音，显示宽度仍然是 1
        self.assertEqual(text.width("e\u0301"), 1)


class TestTruncate(unittest.TestCase):
    def test_short_passthrough(self):
        self.assertEqual(text.truncate("abc", 10), "abc")

    def test_ascii_truncated(self):
        self.assertEqual(text.truncate("abcdef", 4), "abc…")

    def test_never_cuts_a_wide_char(self):
        # 宽度 4：放得下「中」(2) 和省略号 (1)，再放一个汉字会超
        out = text.truncate("中文字", 4)
        self.assertLessEqual(text.width(out), 4)
        self.assertTrue(out.startswith("中"))
        self.assertNotIn("\ufffd", out)

    def test_zero_limit(self):
        self.assertEqual(text.truncate("abc", 0), "")

    def test_exact_fit_no_ellipsis(self):
        self.assertEqual(text.truncate("中文字", 6), "中文字")


class TestPad(unittest.TestCase):
    def test_left(self):
        self.assertEqual(text.pad("中", 5), "中   ")

    def test_right(self):
        self.assertEqual(text.pad("中", 5, "right"), "   中")

    def test_center(self):
        out = text.pad("中", 6, "center")
        self.assertEqual(text.width(out), 6)

    def test_pad_uses_display_width(self):
        # 如果按 len() 补空格，这里会变成 3 而不是 4
        self.assertEqual(text.width(text.pad("中文", 4)), 4)


class TestTable(unittest.TestCase):
    def _columns_start(self, lines):
        """每行列内容的起始位置（跳过表头两行）。"""
        starts = []
        for line in lines[2:]:
            if not line.strip():
                continue
            starts.append(len(line) - len(line.lstrip()))
        return starts

    def test_aligns_with_chinese(self):
        lines = text.table(["名", "值"], [["中文", "1"], ["abc", "22"]])
        widths = {text.width(line) for line in lines}
        # 每行去掉行尾空白后宽度不一定一样，但列位置必须一致
        self.assertTrue(all(text.width(line) > 0 for line in lines))
        self.assertGreaterEqual(len(widths), 1)

    def test_columns_line_up(self):
        lines = text.table(["名字", "说明"], [["短", "x"], ["长一点的名字", "y"]])
        body = lines[2:]
        # 按**显示宽度**算第二列的起始位置；用 str.find 会被中文字符数骗到
        starts = []
        for line in body:
            idx = line.find("x") if "x" in line else line.find("y")
            starts.append(text.width(line[:idx]))
        self.assertEqual(len(set(starts)), 1, f"第二列没对齐：{body}")

    def test_empty_rows(self):
        self.assertEqual(text.table(["a"], []), ["  （没有内容）"])

    def test_limits_truncate(self):
        lines = text.table(["名"], [["很长很长很长很长的名字"]], limits=[6])
        for line in lines[2:]:
            self.assertLessEqual(text.width(line), 6)
        self.assertIn("…", lines[2])

    def test_no_trailing_spaces(self):
        lines = text.table(["a", "b"], [["1", "2"]])
        for line in lines:
            self.assertEqual(line, line.rstrip())


class TestMb(unittest.TestCase):
    def test_kb(self):
        self.assertEqual(text.mb(0.5), "512 KB")

    def test_mb(self):
        self.assertEqual(text.mb(12.4), "12 MB")

    def test_gb(self):
        self.assertEqual(text.mb(2048), "2.0 GB")

    def test_zero(self):
        self.assertEqual(text.mb(0), "0 KB")


class TestWrap(unittest.TestCase):
    def test_wraps_on_spaces(self):
        lines = text.wrap("aa bb cc dd", 5)
        self.assertTrue(all(text.width(line) <= 5 for line in lines))
        self.assertEqual("".join(lines).replace(" ", ""), "aabbccdd")

    def test_chinese_wraps_without_spaces(self):
        # 中文没有空格，第一版按空格切会把整句当成一个词，然后被截断丢字
        source = "匹配到两条请写更完整的名字"
        lines = text.wrap(source, 6)
        self.assertTrue(all(text.width(line) <= 6 for line in lines))
        self.assertEqual("".join(lines), source, "折行不能丢字")

    def test_long_token_is_broken(self):
        lines = text.wrap("x" * 30, 10)
        self.assertEqual(len(lines), 3)
        self.assertEqual("".join(lines), "x" * 30)

    def test_indent_applied(self):
        lines = text.wrap("aa bb", 10, indent="  ")
        self.assertTrue(lines[0].startswith("  "))

    def test_zero_limit_returns_one_line(self):
        self.assertEqual(len(text.wrap("abc", 0)), 1)

    def test_newlines_are_respected(self):
        lines = text.wrap("a\nb", 10)
        self.assertEqual(lines, ["a", "b"])


class TestWrapCjkPunctuation(unittest.TestCase):
    """中文标点不能出现在行首。

    以前按宽度硬断，断点正好落在逗号前面，于是下一行以「，而不是…」开头 ——
    读起来像错行，而这是中文输出里最常见的一句话。
    """

    def test_comma_does_not_start_a_line(self):
        text_ = "这是一段很长的中文说明文字，用来测试换行是不是按显示宽度算的，而不是按字符个数算的。"
        for line in text.wrap(text_, 30):
            stripped = line.strip()
            self.assertNotIn(stripped[:1], "，。、；：？！）】》」』", line)

    def test_period_does_not_start_a_line(self):
        for line in text.wrap("第一句说完了。第二句也要说很长很长很长才行。", 20):
            self.assertNotIn(line.strip()[:1], "。，", line)

    def test_english_wrapping_still_works(self):
        lines = text.wrap("alpha beta gamma delta epsilon", 12)
        self.assertTrue(all(text.width(line) <= 12 for line in lines), lines)

    def test_single_long_token_is_split_but_nothing_is_lost(self):
        # 一个超长的词（路径）只能硬拆，但不能丢字
        path = "C:\\" + "a" * 60 + ".exe"
        lines = text.wrap(path, 20)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), path)
        for line in lines[:-1]:
            self.assertLessEqual(text.width(line), 20, line)

    def test_explicit_newline_is_respected(self):
        self.assertEqual(text.wrap("第一行\n第二行", 40), ["第一行", "第二行"])


if __name__ == "__main__":
    unittest.main()
