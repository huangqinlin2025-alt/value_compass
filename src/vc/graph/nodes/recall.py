"""三路召回 —— 容错隔离的核心落点。

- bm25_recall：关键词精确路，保数字 / 会计科目 / 专有名词字面命中
- vector_recall：语义向量路，保同义泛化
- metadata_recall：元数据过滤路，保范围正确（公司 / 期间 / 章节）

三路通过 LangGraph 的 Send 并行执行，各自被 safe_node 包裹：
任意一路失败只把自己置空并写 errors，绝不拖垮另外两路。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...config import CONFIG
from ...decorators import safe_node
from ...errors import ErrorCode, make_error
from ...ingestion.bm25_index import get_bm25
from ...providers import get_embedding_provider
from ...retrieval.filters import apply_filters_to_chunks, chunk_matches, to_chroma_where
from ...retrieval.vectorstore import get_vector_store
from ...text_utils import coverage_score, token_set


def _query_text(state: Dict[str, Any]) -> str:
    return state.get("query_rewritten") or state.get("query_raw") or ""


@safe_node("bm25_recall", timeout=CONFIG.timeout_bm25, provider="bm25",
           fallback_patch={"recall_bm25": []})
def bm25_recall(state: Dict[str, Any]) -> Dict[str, Any]:
    idx = get_bm25()
    if len(idx) == 0:
        raise ValueError("BM25 索引为空，请先执行入库")
    q = _query_text(state)
    top_k = int((state.get("route_cfg") or {}).get("top_k", CONFIG.top_k_final)) * 3
    filters = state.get("filters") or {}
    # 有过滤条件时先放大候选池：公司名往往不在正文里（页眉被清洗），
    # 只取 top_k 字面命中的话，目标公司的 chunk 可能一条都进不来。
    pool_k = max(top_k, CONFIG.top_k_bm25)
    if filters:
        pool_k = max(pool_k * 5, 200)
    hits = idx.search(q, top_k=pool_k)
    out: List[Dict[str, Any]] = []
    for i, s in hits:
        c = dict(idx.chunks[i])
        c["score"] = float(s)
        c["source_route"] = "bm25"
        out.append(c)
    # 多文档语料下必须做"软"元数据过滤：只靠字面分会让「宁德时代净利润」被
    # 隆基绿能的报表原文顶掉（同一套科目、同样的数字排版）。过滤后若为空则退回
    # 不过滤结果——宁可宽松，也不能把召回打空（沿用"误过滤比不过滤伤害更大"）。
    if filters and out:
        narrowed = apply_filters_to_chunks(out, filters)
        # ui_filters 是绝对过滤条件：宁可空结果也不放宽（放宽 = 答非所问），
        # 自然语言提问仍保留"过滤后为空则退回不过滤"的宽容策略。
        if narrowed:
            out = narrowed
        elif state.get("filters_strict"):
            # strict 且过滤后为空时必须真的置空：若沿用"退回不过滤"的老逻辑，
            # 点"腾讯毛利率"会拿五粮液的报表原文作答（字面分最高的往往是别家公司）。
            out = []
        # 其余（无 strict）保留未过滤结果
    if not hits:
        return {"recall_bm25": [], "errors": [make_error(
            ErrorCode.E_EMPTY_RECALL, "bm25_recall", "BM25 无命中", "rely_on_other_routes")]}
    return {"recall_bm25": out}


@safe_node("vector_recall", timeout=CONFIG.timeout_vector, provider="vectorstore",
           fallback_patch={"recall_vector": []})
def vector_recall(state: Dict[str, Any]) -> Dict[str, Any]:
    store = get_vector_store()
    if store.count() == 0:
        raise ValueError("向量库为空，请先执行入库")
    embedder = get_embedding_provider()
    vec = embedder.embed_query(_query_text(state))
    where = to_chroma_where(state.get("filters") or {})
    top_k = max(CONFIG.top_k_vector, int((state.get("route_cfg") or {}).get("top_k", 8)) * 3)
    items = store.query(vec, top_k=top_k, where=where or None)
    out: List[Dict[str, Any]] = []
    for it in items:
        it["source_route"] = "vector"
        out.append(it)
    return {"recall_vector": out}


@safe_node("metadata_recall", timeout=CONFIG.timeout_bm25, provider="bm25",
           fallback_patch={"recall_meta": []})
def metadata_recall(state: Dict[str, Any]) -> Dict[str, Any]:
    """无过滤条件时本路返回空（属正常，不是错误）。"""
    filters = state.get("filters") or {}
    if not filters:
        return {"recall_meta": []}
    idx = get_bm25()
    q = _query_text(state)
    # 过滤优先（而不是"先 BM25 后过滤"）：见 BM25Index.search_where 的说明。
    hits = idx.search_where(q, lambda c: chunk_matches(c, filters),
                            top_k=max(CONFIG.top_k_bm25 * 3, 60))
    filtered = [dict(idx.chunks[i]) for i, _ in hits]
    if not filtered:
        return {"recall_meta": []}
    q_terms = token_set(q)
    for c in filtered:
        c["score"] = round(coverage_score(q_terms, c.get("text", "")), 4)
        c["source_route"] = "meta"
    filtered.sort(key=lambda x: -x["score"])
    return {"recall_meta": filtered[:CONFIG.top_k_meta]}


@safe_node("rrf_fusion", timeout=1.0, fallback_patch={"fused": [], "fusion_stats": {}})
def rrf_fusion(state: Dict[str, Any]) -> Dict[str, Any]:
    """汇总三路结果，加权 RRF 融合。"""
    from ...retrieval.fusion import fusion_stats, rrf_fuse

    routes = {
        "bm25": state.get("recall_bm25") or [],
        "vector": state.get("recall_vector") or [],
        "meta": state.get("recall_meta") or [],
    }
    cfg = state.get("route_cfg") or {}
    weights = {
        "bm25": float(cfg.get("w_bm25", CONFIG.w_bm25)),
        "vector": float(cfg.get("w_vector", CONFIG.w_vector)),
        "meta": float(cfg.get("w_meta", CONFIG.w_meta)),
    }
    from ...ingestion.bm25_index import get_bm25
    from ...text_utils import strip_page_prefix

    fused = rrf_fuse(routes, weights=weights)
    stats = fusion_stats(fused)

    # IDF 加权相关性闸门：命中罕见词才算真相关，避免常见词把无关问题抬过线
    if fused:
        q_terms = {t for t in (state.get("query_terms") or []) if len(t) >= 2}
        # 取前 5 个候选中最好的相关性：RRF 的第一名未必是字面最相关的那个，
        # 只看 top1 会把"其实召回到了、只是排第二"的正确结果误杀。
        if q_terms:
            rel = max(
                get_bm25().idf_coverage(q_terms, strip_page_prefix(c.get("text", "")))
                for c in fused[:5]
            )
        else:
            rel = 1.0
        stats["top1_relevance"] = round(float(rel), 4)
        stats["relevant"] = bool(rel >= CONFIG.min_top1_relevance)
    else:
        stats["top1_relevance"] = 0.0
        stats["relevant"] = False

    patch: Dict[str, Any] = {"fused": fused, "fusion_stats": stats}
    if not fused:
        patch["errors"] = [make_error(
            ErrorCode.E_EMPTY_RECALL, "rrf_fusion", "三路召回均无结果", "fallback")]
    elif stats["top1_score"] < CONFIG.low_score_min:
        patch["errors"] = [make_error(
            ErrorCode.E_LOW_SCORE, "rrf_fusion",
            "top1 融合分 %.4f 低于阈值 %.4f" % (stats["top1_score"], CONFIG.low_score_min),
            "fallback")]
    elif not stats["relevant"]:
        patch["errors"] = [make_error(
            ErrorCode.E_LOW_SCORE, "rrf_fusion",
            "top1 IDF 相关性 %.3f 低于阈值 %.3f" % (stats["top1_relevance"], CONFIG.min_top1_relevance),
            "fallback")]
    return patch


def successful_routes(state: Dict[str, Any]) -> List[str]:
    return [r for r in ("bm25", "vector", "meta") if state.get("recall_" + r)]
