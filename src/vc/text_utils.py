"""中文文本处理工具：归一化、分词（供 BM25 / Hashing Embedding / 覆盖度打分共用）。

金融文本的特点：数字、单位、会计科目名必须逐字命中，因此分词策略为
「数字 + 拉丁词 + 汉字 unigram + 汉字 bigram」，不依赖任何外部分词器。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import List, Set

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-zA-Z][a-zA-Z\.\-]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_MONEY = re.compile(r"\d+(?:\.\d+)?\s*(?:亿元|万元|千元|元|万美元|万美元|亿美元|美元|%)")

_STOP = set(
    "的了和与及是在为对从中把被并等或者就都很也还要会能可将把这让给于以上下其之"
    "，。；：、“”‘’（）()[]{}《》〈〉-—_/\\|＋＋　 \t\r\n"
)


def normalize_text(text: str) -> str:
    """全角转半角、统一空白、去掉零宽字符。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tokenize(text: str) -> List[str]:
    if not text:
        return []
    s = normalize_text(text).lower()
    toks: List[str] = []
    toks.extend(_MONEY.findall(s))
    toks.extend(_NUMBER.findall(s))
    toks.extend(t.lower() for t in _LATIN.findall(s))
    cjk = "".join(_CJK.findall(s))
    toks.extend(list(cjk))
    toks.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
    return [t for t in toks if t and t not in _STOP]


def token_set(text: str) -> Set[str]:
    return set(tokenize(text))


def extract_numbers(text: str) -> List[str]:
    """抽取带单位/百分号的数值字面量，用于数值一致性校验。"""
    if not text:
        return []
    s = normalize_text(text)
    nums = _MONEY.findall(s) + _NUMBER.findall(s)
    return [n.strip() for n in nums if n.strip()]


def coverage_score(query_terms: Set[str], text: str) -> float:
    """查询词在文本中的覆盖率。"""
    if not query_terms:
        return 0.0
    doc = token_set(text)
    if not doc:
        return 0.0
    hit = len(query_terms & doc)
    return hit / float(len(query_terms))


_PREFIX_LINE = re.compile(r"^\[P\d+[｜|][^\]]*\]\s*")


def strip_page_prefix(text: str) -> str:
    """去掉 chunk 首部的 "[P12|章节路径]" 前缀。

    该前缀是为检索与溯源加的，做覆盖率/相关性判断时必须剔除，
    否则章节名里的字会被算作命中，导致相关性虚高。
    """
    if not text:
        return ""
    out = _PREFIX_LINE.sub("", text, count=1)
    return out


def clean_display(text: str) -> str:
    """展示用清洗：去掉正文里夹带的 "[P12|章节]" 前缀行（合并小块时可能重复出现）。"""
    if not text:
        return ""
    return re.sub(r"\[P\d+[｜|][^\]]*\]\s*", "", text)


def stable_hash(text: str) -> str:
    return hashlib.md5(normalize_text(text).encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """粗估 token 数（中文约 1 字 0.6 token，英文约 4 字符 1 token）。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = max(0, len(text) - cjk)
    return int(cjk * 0.6 + other / 4.0) + 1


def clip(text: str, limit: int = 200) -> str:
    """截断到不超过 limit（省略号占位算在额度内）。

    契约是"结果长度 <= limit"：若先取 limit 个字符再补省略号，实际长度是 limit+1，
    下游按 <= limit 做硬约束的调用方（如星图卡片 100 字上限）会因此超标一个字符。
    """
    t = normalize_text(text)
    if len(t) <= limit:
        return t
    return t[:limit] if limit <= 1 else t[:limit - 1] + "…"
