"""入库子图编排：与问答主图解耦，在线只读索引快照，入库失败不影响在线可用性。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from langgraph.graph import END, START, StateGraph

from ..observability import new_trace_id
from ..state import IngestState
from .nodes.ingest_nodes import (
    build_bm25,
    clean_normalize,
    dedup_hash,
    embed_upsert,
    enrich_metadata,
    load_pdf_node,
    plan_diff_node,
    purge_stale,
    skip_node,
    table_aware_split,
    write_manifest,
)


def route_after_load(state: Dict[str, Any]) -> str:
    if not state.get("pages"):
        return "end"
    return "clean_normalize"


def route_after_plan(state: Dict[str, Any]) -> str:
    plan = state.get("diff_plan") or {}
    if plan.get("mode") == "skip":
        return "skip_node"
    return "embed_upsert"


def build_ingest_graph() -> Any:
    g = StateGraph(IngestState)
    g.add_node("purge_stale", purge_stale)
    g.add_node("load_pdf", load_pdf_node)
    g.add_node("clean_normalize", clean_normalize)
    g.add_node("enrich_metadata", enrich_metadata)
    g.add_node("table_aware_split", table_aware_split)
    g.add_node("dedup_hash", dedup_hash)
    g.add_node("plan_diff", plan_diff_node)
    g.add_node("skip_node", skip_node)
    g.add_node("embed_upsert", embed_upsert)
    g.add_node("build_bm25", build_bm25)
    g.add_node("write_manifest", write_manifest)

    g.add_edge(START, "purge_stale")
    g.add_edge("purge_stale", "load_pdf")
    g.add_conditional_edges("load_pdf", route_after_load, {"clean_normalize": "clean_normalize", "end": END})
    g.add_edge("clean_normalize", "enrich_metadata")
    g.add_edge("enrich_metadata", "table_aware_split")
    g.add_edge("table_aware_split", "dedup_hash")
    g.add_edge("dedup_hash", "plan_diff")
    g.add_conditional_edges("plan_diff", route_after_plan, {"skip_node": "skip_node", "embed_upsert": "embed_upsert"})
    g.add_edge("skip_node", END)
    g.add_edge("embed_upsert", "build_bm25")
    g.add_edge("build_bm25", "write_manifest")
    g.add_edge("write_manifest", END)
    return g.compile()


_GRAPH: Optional[Any] = None


def get_ingest_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_ingest_graph()
    return _GRAPH


def ingest(doc_path: str = None, force_rebuild: bool = False,
           skip_bm25: bool = False) -> Dict[str, Any]:
    """单文档入库。

    skip_bm25=True 只跳过 BM25 重建（批量入库用），manifest 照写，
    因此仍可断点续跑；批量结束后必须调用 rebuild_bm25()，否则在线侧读到旧索引。
    """
    from ..config import CONFIG

    state: Dict[str, Any] = {
        "doc_path": doc_path or str(CONFIG.default_pdf),
        "force_rebuild": force_rebuild,
        "skip_bm25": skip_bm25,
        "trace_id": new_trace_id(),
    }
    return get_ingest_graph().invoke(state)


def rebuild_bm25() -> Dict[str, Any]:
    """按 manifest 中全部 active 文档重建一次 BM25，并刷新进程内缓存。"""
    from ..ingestion.bm25_index import BM25Index, get_bm25
    from ..ingestion.manifest import active_doc_ids, read_manifest
    from ..ingestion.snapshot import load_all_snapshots

    ids = active_doc_ids(read_manifest())
    chunks = load_all_snapshots(ids)
    idx = BM25Index(chunks)
    idx.save()
    get_bm25(refresh=True)
    return {"docs": len(ids), "chunks": len(chunks)}
