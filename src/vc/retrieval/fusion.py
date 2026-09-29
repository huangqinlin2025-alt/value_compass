"""多路召回融合：加权 RRF（Reciprocal Rank Fusion）。

为什么用 RRF 而不是分数直接相加：三路（BM25 / 向量 / 元数据）的分数尺度不可比，
RRF 只用排名，天然免疫量纲问题，且对单路噪声更鲁棒。

score(d) = Σ_i w_i / (k + rank_i(d))
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from ..config import CONFIG


def rrf_fuse(
    routes: Dict[str, Sequence[Dict[str, Any]]],
    weights: Dict[str, float] = None,
    k: int = None,
    table_boost: float = None,
    top_k: int = None,
) -> List[Dict[str, Any]]:
    """routes: {"bm25": [chunk...], "vector": [...], "meta": [...]}，按列表顺序即排名。"""
    k = CONFIG.rrf_k if k is None else k
    weights = weights or {"bm25": CONFIG.w_bm25, "vector": CONFIG.w_vector, "meta": CONFIG.w_meta}
    table_boost = CONFIG.table_boost if table_boost is None else table_boost
    top_k = top_k or CONFIG.top_k_final * 4

    scores: Dict[str, float] = {}
    merged: Dict[str, Dict[str, Any]] = {}
    for route, items in (routes or {}).items():
        w = float(weights.get(route, 1.0))
        for rank, c in enumerate(items or []):
            cid = c.get("chunk_id") or c.get("content_hash")
            if not cid:
                continue
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank + 1)
            if cid not in merged:
                merged[cid] = dict(c)
            else:
                merged[cid].setdefault("_routes", []).append(route)
            # 记录该片段在每一路中的名次，供 UI 解释"为什么是它"
            merged[cid].setdefault("_ranks", {})[route] = rank + 1

    out: List[Dict[str, Any]] = []
    for cid, s in scores.items():
        item = dict(merged[cid])
        if item.get("table_flag"):
            s *= table_boost
        item["score"] = round(float(s), 6)
        routes_hit = sorted(set([item.get("source_route", "")] + item.pop("_routes", [])))
        item["routes"] = [r for r in routes_hit if r]
        ranks = item.pop("_ranks", {})
        if ranks:
            item["rank_by_route"] = dict(sorted(ranks.items(), key=lambda x: x[1]))
        out.append(item)

    out.sort(key=lambda x: -x["score"])
    return out[:top_k]


def fusion_stats(fused: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not fused:
        return {"count": 0, "top1_score": 0.0, "table_hits": 0, "pages": [],
                "route_hits": {}, "multi_route_hits": 0}
    route_hits: Dict[str, int] = {}
    multi = 0
    for c in fused:
        routes = c.get("routes") or ([c["source_route"]] if c.get("source_route") else [])
        if len(routes) > 1:
            multi += 1
        for r in routes:
            route_hits[r] = route_hits.get(r, 0) + 1
    return {
        "count": len(fused),
        "top1_score": float(fused[0].get("score", 0.0)),
        "table_hits": sum(1 for c in fused if c.get("table_flag")),
        "pages": sorted({c.get("page") for c in fused if c.get("page") is not None})[:10],
        "route_hits": route_hits,
        "multi_route_hits": multi,
    }
