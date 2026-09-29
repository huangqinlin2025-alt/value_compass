"""清洗归一化：页眉页脚 / 页码 / 空行 / 全角 / 水印 / 符号字体。

思路：财报 PDF 每页都重复同样的页眉（公司名+报告名）与页脚（页码），
这些噪声会同时污染 BM25 的词频统计和向量的语义表示，必须在切分前剔除。
判定方式：跨页统计短行出现频率，出现率超过 30% 的短行视为页眉页脚。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import List, Set

from ..state import Page
from ..text_utils import normalize_text

_PURE_NUMBER = re.compile(r"^\s*[-—–]*\s*\d+\s*[-—–]*\s*$")
_PAGE_MARK = re.compile(r"^\s*第?\s*\d+\s*页?(\s*/?\s*共?\s*\d+\s*页?)?\s*$")
_WATERMARK = re.compile(r"(内部资料|仅供|机密|confidential)", re.I)

# 符号字体归一：财报用 Wingdings 类图标字体画勾选框与项目符号，PDF 抽取后落在 Unicode 私用区。
# 全语料实测命中 6 个码位（出现次数）：
#   U+F052(2590) / U+F0FE(17) / U+F0A3(10) = 已勾选方框，上下文形如 "□适用 ☑不适用"、"☑是 □否"
#   U+F0B7(334) / U+F06E(36) / U+F06C(28) = 项目符号，上下文形如 "· 第一层次输入值是…"
# 私用区字符对 tokenizer 是纯噪声：进不了汉字 unigram/bigram，却会白占 BM25 词频与向量维度，
# 因此归一成 Unicode 记号，保留"勾了哪一项"的语义；未识别的私用区字符整枚丢弃（换成空格防粘连）。
_PUA_CHECKED = re.compile(r"[\uf052\uf0fe\uf0a3]")
_PUA_BULLET = re.compile(r"[\uf0b7\uf06e\uf06c]")
_PUA_OTHER = re.compile(r"[\ue000-\uf8ff]")


def normalize_icons(text: str) -> str:
    """把落在私用区的勾选框 / 项目符号换成 Unicode 记号。

    必须在 normalize_text **之前**调用：丢弃未知私用区字符换成的空格，
    要靠后续的全角转半角与空白压缩一起收干净。
    """
    if not text:
        return text
    text = _PUA_CHECKED.sub("☑", text)
    text = _PUA_BULLET.sub("·", text)
    return _PUA_OTHER.sub(" ", text)


def _learn_header_footer(pages: List[Page]) -> Set[str]:
    counter: Counter = Counter()
    for pg in pages:
        for line in (pg.get("text") or "").splitlines():
            line = line.strip()
            if 0 < len(line) <= 40:
                counter[line] += 1
    threshold = max(3, int(len(pages) * 0.3))
    return {line for line, cnt in counter.items() if cnt >= threshold}


def clean_page_text(text: str, boiler: Set[str]) -> str:
    keep: List[str] = []
    prev = None
    for raw in (text or "").splitlines():
        line = normalize_text(normalize_icons(raw))
        if not line:
            continue
        if _PURE_NUMBER.match(line) or _PAGE_MARK.match(line):
            continue
        if _WATERMARK.search(line):
            continue
        if line in boiler:
            continue
        if line == prev:  # 连续重复行（常见于双栏 PDF 抽取）
            continue
        keep.append(line)
        prev = line
    return "\n".join(keep)


def clean_pages(pages: List[Page]) -> List[Page]:
    # 先归一图标再学页眉页脚：否则"学到的 boiler 行 vs 清洗后的行"会因符号字体对不上，页眉漏删
    normed: List[Page] = []
    for pg in pages:
        item = dict(pg)
        item["text"] = normalize_icons(pg.get("text", ""))
        normed.append(item)  # type: ignore[arg-type]
    boiler = _learn_header_footer(normed)
    out: List[Page] = []
    for pg in normed:
        cleaned = clean_page_text(pg.get("text", ""), boiler)
        item = dict(pg)
        item["text"] = cleaned
        out.append(item)  # type: ignore[arg-type]
    return out
