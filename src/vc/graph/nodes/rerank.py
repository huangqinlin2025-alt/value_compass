"""rerank / context_compress / retry_shrink。

rerank 是"可降级"节点：失败或超时就用 RRF 顺序继续，绝不中断主链路。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...config import CONFIG
from ...decorators import safe_node
from ...providers import get_reranker
from ...text_utils import estimate_tokens


@safe_node("rerank", timeout=CONFIG.timeout_rerank, provider="rerank",
           fallback_patch={"reranked": []})
def rerank(state: Dict[str, Any]) -> Dict[str, Any]:
    fused = list(state.get("fused") or [])
    if not fused:
        return {"reranked": []}
    cfg = state.get("route_cfg") or {}
    q = state.get("query_rewritten") or state.get("query_raw") or ""
    top_k = max(CONFIG.top_k_final, int(cfg.get("top_k", CONFIG.top_k_final)) * 2)

    # 排序不改集合：召回不足时其它期照样能补位，不会像硬过滤那样把召回打空
    prefer_period = str((state.get("ui_filters") or {}).get("prefer_period") or "")
    prefer_table = bool(cfg.get("prefer_table"))

    def _order(c: Dict[str, Any]) -> tuple:
        return (
            # 期间聚焦：同一公司有 6 期报告，跨期混喂会让模型把"这一期的金额"安到
            # "另一期的口径"上，数值一致性闸门据此判 E_NUM_MISMATCH，
            # 整张卡片直接转兜底（实测 business_mix 就是这样惜答的）。
            bool(prefer_period) and str(c.get("report_period") or "") != prefer_period,
            # 表格优先：table chunk 提前，保证数字类问题先拿到整表
            prefer_table and not c.get("table_flag"),
            -float(c.get("score", 0.0)),
        )

    if prefer_period or prefer_table:
        fused = sorted(fused, key=_order)

    reranker = get_reranker()
    ranked = reranker.rerank(q, fused, top_k=top_k)

    patch: Dict[str, Any] = {"reranked": ranked or list(fused[:top_k])}
    # 真实重排模型不可用时工厂已回落到启发式，这里显式标记，UI 才能解释"为什么排序变差了"
    if getattr(reranker, "fallback_reason", ""):
        patch["degraded"] = ["rerank:fallback_heuristic"]
    return patch


@safe_node("context_compress", timeout=1.0, fallback_patch={"context": []})
def context_compress(state: Dict[str, Any]) -> Dict[str, Any]:
    """按 token 预算从高分往低分填充，去重，保留页码与表格标记。"""
    ranked = list(state.get("reranked") or state.get("fused") or [])
    # 同期聚焦：主导期片段够 2 条就只喂这一期。
    # 排序（rerank 里的期间聚焦）只把同期片段提前，token 预算一大其它期照样挤进来，
    # 模型仍会把不同期的金额并排放进卡片 -> 数值闸门 E_NUM_MISMATCH -> 整卡转兜底。
    # 门槛设 2 条是安全垫：同期资料不足时宁可保留混合，也不让上下文变得太薄。
    ui = state.get("ui_filters") or {}
    periods = [str(p) for p in (ui.get("report_periods") or []) if str(p)]
    if len(periods) >= 2:
        # 对比模式：按期间**交替**取，保证两期都进上下文。
        # 沿用上面的"只留主导期"会让对比退化成单期——只见一期数字却要输出同比，
        # 模型只能编一个数，而数值闸门查不出：那个数在原文里确实存在。
        buckets = {p: [c for c in ranked if str(c.get("report_period") or "") == p] for p in periods}
        merged: List[Dict[str, Any]] = []
        while any(buckets.values()):
            for p in periods:
                if buckets[p]:
                    merged.append(buckets[p].pop(0))
        ranked = merged
    else:
        prefer = str(ui.get("prefer_period") or "")
        if prefer:
            same = [c for c in ranked if str(c.get("report_period") or "") == prefer]
            if len(same) >= 2:
                ranked = same
    budget = CONFIG.context_token_budget
    seen = set()
    picked: List[Dict[str, Any]] = []
    used = 0
    for c in ranked:
        key = c.get("chunk_id") or c.get("content_hash") or c.get("text", "")[:64]
        if key in seen:
            continue
        seen.add(key)
        cost = estimate_tokens(c.get("text", ""))
        if used + cost > budget:
            continue
        picked.append(c)
        used += cost
    if not picked and ranked:
        picked = ranked[:3]
    return {"context": picked}


@safe_node("retry_shrink", timeout=0.5)
def retry_shrink(state: Dict[str, Any]) -> Dict[str, Any]:
    """数值校验失败后的收紧重试：只保留分数最高的前一半片段。"""
    ctx = list(state.get("context") or [])
    keep = max(2, len(ctx) // 2) if ctx else 0
    return {"context": ctx[:keep], "retry_count": int(state.get("retry_count") or 0) + 1}
