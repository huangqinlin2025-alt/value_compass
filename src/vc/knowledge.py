"""知识库元信息门面：对外暴露"当前库里有什么"，供路由、过滤、兜底话术共用。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import CONFIG
from .ingestion.manifest import active_doc_ids, read_manifest


def all_docs() -> List[Dict[str, Any]]:
    m = read_manifest()
    docs = [d for d in m.get("docs", []) if d.get("status", "active") == "active"]
    return docs or []


def primary_doc() -> Dict[str, Any]:
    docs = all_docs()
    return docs[-1] if docs else {}


def known_meta() -> Dict[str, Any]:
    """供过滤同义归一使用。

    多文档语料下必须把**全部**文档的公司/行业暴露出来：只给最后一家的信息，
    "宁德时代净利润" 这类问题就会因为 known 里只有茅台而抽不出 stock_code。
    """
    docs = all_docs()
    d = docs[-1] if docs else {}
    return {
        "company": d.get("company", ""),
        "stock_code": d.get("stock_code", ""),
        "report_period": d.get("report_period", ""),
        "short_name": d.get("short_name", ""),
        "doc_version": d.get("doc_version", ""),
        "docs": [
            {"company": x.get("company", ""), "short_name": x.get("short_name", ""),
             "stock_code": x.get("stock_code", ""), "industry": x.get("industry", "")}
            for x in docs
        ],
        "industries": sorted({x.get("industry", "") for x in docs if x.get("industry")}),
    }


def doc_count() -> int:
    return len(all_docs())


def company_count() -> int:
    return len({d.get("stock_code", "") for d in all_docs() if d.get("stock_code")})


# 报告期的先后：Q1 < H1 < Q3 < A（年报覆盖全年，排最后）
_PERIOD_ORDER = {"Q1": 1, "H1": 2, "Q3": 3, "A": 4}


def _period_key(p: str) -> int:
    """报告期 -> 可比较的序号（跨年也能正确排序）。"""
    s = str(p or "").strip().upper()
    if len(s) < 4 or not s[:4].isdigit():
        return -1
    return int(s[:4]) * 10 + _PERIOD_ORDER.get(s[4:], 0)


def company_options() -> List[Dict[str, str]]:
    """库内可选公司（按代码去重），供"未指定公司时提示用户选择"使用。

    星图点击必须锁定单一公司：语料是 10 家 × 6 期，不限定的话三路召回会跨公司
    混进不同主体的片段，卡片只能归纳出"XX 是指……"的通用定义，且页码在多份
    文档间是歧义 ID（P2 同时属于好几家公司）。
    """
    seen: Dict[str, Dict[str, str]] = {}
    for d in all_docs():
        code = str(d.get("stock_code") or "")
        if not code or code in seen:
            continue
        seen[code] = {
            "stock_code": code,
            "company": str(d.get("company") or ""),
            "short_name": str(d.get("short_name") or ""),
        }
    return [seen[c] for c in sorted(seen)]


def latest_period(stock_code: str = "") -> str:
    """该公司的最新报告期；未指定公司时取全库最新一期。取不到返回空串。"""
    best, best_key = "", -1
    for d in all_docs():
        if stock_code and str(d.get("stock_code")) != str(stock_code):
            continue
        p = str(d.get("report_period") or "")
        k = _period_key(p)
        if k > best_key:
            best, best_key = p, k
    return best


def default_periods(stock_code: str = "", want: int = 2) -> List[str]:
    """默认对比期间：最新一期 + 它的**去年同期**（同口径，用于同比）。

    为什么必须是同口径：拿 2025A（全年）对 2024H1（半年）会把半年数和全年数并排，
    模型算出的"同比 -50%"是假的——数值闸门查不出这个错，因为它确实能在原文里逐字
    找到这两个数。取不到去年同期时宁可退化成次新的一期，也不跨尾缀（A/H1/Q1）配对。
    """
    want = max(1, int(want or 1))
    ps: List[str] = []
    for d in all_docs():
        if stock_code and str(d.get("stock_code")) != str(stock_code):
            continue
        p = str(d.get("report_period") or "")
        if p and p not in ps:
            ps.append(p)
    ps.sort(key=_period_key, reverse=True)
    if not ps:
        return []
    out = [ps[0]]
    if want > 1 and len(ps) > 1:
        latest = ps[0]
        prev = "%d%s" % (int(latest[:4]) - 1, latest[4:]) if latest[:4].isdigit() else ""
        if prev and prev in ps:
            out.append(prev)                      # 去年同期
        else:
            out += [p for p in ps[1:] if p not in out][:1]   # 退化：次新的一期
    return out[:want]


def name_of(stock_code: str) -> str:
    """代码 -> 公司简称（展示用），查不到返回空串。"""
    for d in all_docs():
        if str(d.get("stock_code")) == str(stock_code):
            return str(d.get("short_name") or d.get("company") or "")
    return ""


def report_title() -> str:
    docs = all_docs()
    if not docs:
        return "知识库"
    if len(docs) > 1:
        # 注意：这个标题会拼进兜底话术，而兜底话术要走数值一致性闸门——
        # 标题里出现"61 份"这类数字会被判成"答案数字不在原文中"的假阳性
        # （实测把 22 题评测的幻觉率从 0 拉到 9.1%）。所以只给不含数字的定性描述。
        return "知识库（多家上市公司定期报告）"
    d = docs[-1]
    period = d.get("report_period", "") or ""
    company = d.get("company", "") or "报告"
    return "《%s%s报告》" % (company, _period_cn(period))


def _period_cn(period: str) -> str:
    if not period:
        return ""
    tail = period[-2:]
    year = period[:4]
    return {"H1": "%s年半年度" % year, "A": "%s年年度" % year,
            "Q1": "%s年一季度" % year, "Q3": "%s年三季度" % year}.get(tail, "")


def total_chunks() -> int:
    return sum(int(d.get("chunk_count", 0) or 0) for d in all_docs())


def is_ready() -> bool:
    return bool(active_doc_ids(read_manifest())) and CONFIG.bm25_path.exists()


SUGGESTIONS: List[str] = [
    "公司本期营业收入是多少？",
    "归属于上市公司股东的净利润同比变化多少？",
    "资产负债率是多少？",
    "经营活动产生的现金流量净额是多少？",
    "主营业务构成情况如何？",
]
