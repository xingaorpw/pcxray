"""终端文本排版：按**显示宽度**对齐。

中文、全角标点、emoji 在终端里占两格。直接用 `len()` 补空格的话，表格会歪成
斜的 —— 这是中文 CLI 最常见的观感问题，而且很容易被忽略，因为开发者的终端里
经常混着等宽字体和英文。
"""

from __future__ import annotations

import math

import unicodedata

__all__ = ["width", "pad", "truncate", "table", "wrap"]


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(text: str) -> int:
    return sum(_char_width(ch) for ch in str(text))


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，不切断半个宽字符。"""
    text = str(text)
    if limit <= 0:
        return ""
    if width(text) <= limit:
        return text
    budget = limit - width(ellipsis)
    out = []
    used = 0
    for ch in text:
        w = _char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def pad(text: str, limit: int, align: str = "left") -> str:
    # 宽度 <= 0 时没有「补到多宽」这回事，原样返回。走 truncate 的话
    # 内容会被整段吞掉 —— 静默丢数据比显示不齐更糟。
    if limit <= 0:
        return text
    text = truncate(text, limit)
    gap = limit - width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def table(
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str] | None = None,
    limits: list[int] | None = None,
) -> list[str]:
    """把二维数据排成对齐的文本表。

    `limits` 给每列的最大显示宽度。终端宽度有限，名字不截断会把整行撑爆，
    后面的列全部跑到屏幕外面去 —— 那比不显示还糟。
    """
    if not rows:
        return ["  （没有内容）"]
    cols = len(headers)
    aligns = aligns or ["left"] * cols
    if limits:
        headers = [truncate(headers[i], limits[i]) if i < len(limits) else headers[i] for i in range(cols)]
        rows = [
            [truncate(row[i], limits[i]) if i < len(limits) and i < len(row) else (row[i] if i < len(row) else "") for i in range(cols)]
            for row in rows
        ]
    widths = [width(h) for h in headers]
    for row in rows:
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], width(cell))
    # 最后一列不补空格，避免行尾一串空白
    lines = []
    header = "  ".join(pad(headers[i], widths[i], aligns[i]) for i in range(cols)).rstrip()
    lines.append(header)
    lines.append("  ".join("─" * widths[i] for i in range(cols)))
    for row in rows:
        cells = []
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            cells.append(pad(cell, widths[i], aligns[i]))
        lines.append("  ".join(cells).rstrip())
    return lines


# 这些字符不能出现在一行的开头。中文排版里叫「禁则字符」。
_NO_LINE_START = set("，。、；：？！）】》」』%…·’”")


def wrap(text: str, limit: int, indent: str = "") -> list[str]:
    """按显示宽度折行。

    中文没有空格，所以不能只按空格切 —— 那样一整句会被当成一个「词」，结果
    要么整行超宽，要么被截断丢字。这里按字符宽度贪心填，填满时优先回退到
    最近的空格（英文好看），退不回去就硬断（中文正常）。
    """
    if limit <= 0:
        return [indent + str(text)]
    lines: list[str] = []
    for raw in str(text).split("\n"):
        current = ""
        used = 0
        for ch in raw:
            w = _char_width(ch)
            if used + w > limit and current:
                cut = current.rfind(" ")
                if cut > limit // 3:
                    lines.append(current[:cut])
                    current = current[cut + 1 :]
                    used = width(current)
                else:
                    lines.append(current)
                    current, used = "", 0
            # 中文标点不能出现在行首（「，而不是…」这种读起来像错行）。遇到这种
            # 字符就让它跟着上一行走，宁可这一行超一个字的宽度。
            if not current and ch in _NO_LINE_START and lines:
                lines[-1] += ch
                continue
            current += ch
            used += w
        lines.append(current)
    return [indent + line for line in lines]


def mb(value: float) -> str:
    """内存的易读写法。

    两个坑：`1023.9` 会被 `:.0f` 四舍五入成「1024 MB」（该进位成 GB 了），
    负数走 `< 1` 分支会显示成「-1024 KB」。单位判断要看**显示出来的**数，
    不能看原始数。
    """
    if not math.isfinite(value):
        # nan / inf 不该出现（都是相加来的），但真出现时别把 "nan KB" 甩给用户
        return "—"
    if abs(value) >= 1023.5:  # 会四舍五入成 1024 MB，直接进位
        return f"{value / 1024:.1f} GB"
    if abs(value) >= 1:
        return f"{value:.0f} MB"
    return f"{value * 1024:.0f} KB"
