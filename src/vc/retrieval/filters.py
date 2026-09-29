"""从用户问题中抽取元数据过滤条件（第三路召回的输入）。

抽取维度：公司/代码、报告期间（2026H1 / 2025A）、章节关键词、是否必须命中表格。
抽取不到就返回空过滤（不强行过滤，避免误伤召回）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..text_utils import normalize_text

_SECTION_KEYWORDS = {
    "财务": ["主要会计数据", "财务指标"],
    "现金流": ["现金流"],
    "资产负债": ["资产负债"],
    "利润": ["利润表", "利润"],
    "股东": ["股东"],
    "治理": ["公司治理"],
    "风险": ["风险"],
    "审计": ["审计"],
}

# 坑：原先写成 `(20\d{2})\s*年?\s*年报|年度报告`——`|` 的优先级让后半支成为独立分支，
# 于是「2024年年度报告」命中 `年度报告` 且年份分组为 None，拼出 `NoneA`，
# 这个条件在库里永远匹配不到，等于静默禁掉了期间过滤。年份一律强制捕获。
_PERIOD_PATTERNS = [
    (re.compile(r"(20\d{2})\s*年?\s*半\s*年"), "H1"),
    (re.compile(r"(20\d{2})\s*年?\s*年\s*度"), "A"),
    (re.compile(r"(20\d{2})\s*年?\s*(一|1)\s*季"), "Q1"),
    (re.compile(r"(20\d{2})\s*年?\s*(三|3)\s*季"), "Q3"),
]
_CODE_RE = re.compile(r"\b(\d{6})\b")
_COMPANY_RE = re.compile(r"([\u4e00-\u9fff]{2,10})(?:股份有限公司|公司)")

# 行业触发词（key 与 sidecar/manifest 的 industry 字段一致）
# 只在问句真的提到行业时才过滤；抽不到就不过滤，避免误杀跨公司对比类问题。
_INDUSTRY_KEYWORDS: Dict[str, List[str]] = {
    "白酒": ["白酒", "酒类", "酒企"],
    "银行": ["银行", "股份制银行"],
    "保险": ["保险"],
    "新能源": ["新能源", "电池", "锂电", "动力电池"],
    "光伏": ["光伏", "组件", "硅片"],
    "电子": ["电子", "安防", "视频监控"],
    "食品": ["食品", "乳品", "乳制品", "饮料"],
    "汽车": ["汽车", "乘用车", "整车"],
    "医药": ["医药", "医疗", "CXO", "创新药"],
}


def _match_industry(q: str, known_industries: List[str] = None) -> str:
    """按触发词匹配行业；known_industries 非空时只认库里已有的行业，防止抽到不存在的维度。"""
    for ind, words in _INDUSTRY_KEYWORDS.items():
        if known_industries and ind not in known_industries:
            continue
        if ind in q or any(w in q for w in words):
            return ind
    return ""


def extract_filters(query: str, known: Dict[str, Any] = None) -> Dict[str, Any]:
    """known 为 manifest 中已知的公司/股票代码/期间，用于做同义归一。"""
    known = known or {}
    q = normalize_text(query)
    f: Dict[str, Any] = {}

    m = _CODE_RE.search(q)
    if m:
        f["stock_code"] = m.group(1)

    if not f.get("stock_code"):
        # 多文档语料：known 带 docs 列表时逐家比对，命中任何一家即锁定其代码
        docs = [d for d in (known.get("docs") or []) if isinstance(d, dict)]
        if not docs and (known.get("company") or known.get("short_name")):
            docs = [known]
        for d in docs:
            short = str(d.get("short_name") or "")
            known_company = str(d.get("company") or "")
            if (short and short in q) or (known_company and known_company[:4] and known_company[:4] in q):
                if d.get("stock_code"):
                    f["stock_code"] = d.get("stock_code", "")
                    break

    industry = _match_industry(q, list(known.get("industries") or []))
    if industry:
        f["industry"] = industry

    for rx, tag in _PERIOD_PATTERNS:
        m = rx.search(q)
        if m and m.group(1):
            f["report_period"] = "%s%s" % (m.group(1), tag)
            break
    if not f.get("report_period") and re.search(r"半年|中报|H1", q):
        f["report_period"] = known.get("report_period", "")

    for kw, sections in _SECTION_KEYWORDS.items():
        if kw in q:
            f["section_keywords"] = sections
            break

    if re.search(r"同比|增长率|占比|比例|率", q) or re.search(r"表|明细|构成", q):
        f["prefer_table"] = True

    return {k: v for k, v in f.items() if v not in (None, "", [], {})}


def chunk_matches(chunk: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    """单条 chunk 是否满足等值过滤（section/prefer_table 是软偏好，不参与硬过滤）。"""
    if not filters:
        return True
    for k in ("stock_code", "industry"):
        if filters.get(k) and str(chunk.get(k, "")) != str(filters[k]):
            return False
    # 期间：对比模式给的是 report_periods（多期，OR 命中），单期仍是等值匹配。
    # 两者互斥——同时出现时以多期为准，否则"两期片段"会被单期等值过滤掉一半。
    periods = [str(p) for p in (filters.get("report_periods") or []) if str(p)]
    if periods:
        return str(chunk.get("report_period", "")) in periods
    if filters.get("report_period") and str(chunk.get("report_period", "")) != str(filters["report_period"]):
        return False
    return True


def apply_filters_to_chunks(chunks: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """对 BM25 / 融合结果做后置过滤（向量路用 Chroma 的 where）。"""
    if not filters:
        return list(chunks)
    return [c for c in chunks if chunk_matches(c, filters)]


def to_chroma_where(filters: Dict[str, Any]) -> Dict[str, Any]:
    """转成 Chroma `where`。

    坑：本机 Chroma 版本**不接受隐式多键 AND**（`{"stock_code":..,"report_period":..}`
    会抛 `Expected where to have exactly one operator`），多条件必须显式包成 `$and`。
    "某公司某年财报"这类问题会同时抽到两个条件，所以这里是必踩路径。
    """
    pairs: List[Dict[str, Any]] = []
    for k in ("stock_code", "industry", "table_flag"):
        if k in filters:
            pairs.append({k: filters[k]})
    periods = [str(p) for p in (filters.get("report_periods") or []) if str(p)]
    if len(periods) > 1:
        pairs.append({"$or": [{"report_period": p} for p in periods]})
    elif len(periods) == 1:
        pairs.append({"report_period": periods[0]})
    elif filters.get("report_period"):
        pairs.append({"report_period": filters["report_period"]})
    if not pairs:
        return {}
    if len(pairs) == 1:
        return pairs[0]
    return {"$and": pairs}
