"""入库子图节点：load -> clean -> enrich -> split -> hash -> plan -> upsert -> bm25 -> manifest。

顺序说明：先 enrich（抽取公司/期间等文档级元数据）再 split，
让每个 chunk 从诞生起就带完整的 section_path 与元数据，避免二次遍历。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List

from ...config import CONFIG
from ...decorators import safe_node
from ...errors import ErrorCode, make_error
from ...ingestion.bm25_index import BM25Index
from ...ingestion.cleaner import clean_pages
from ...ingestion.loader import file_fingerprint, load_pdf
from ...ingestion.manifest import (
    active_doc_ids,
    get_doc,
    next_version,
    plan_diff,
    read_manifest,
    upsert_doc,
    write_manifest_atomic,
)
from ...ingestion.metadata import detect_doc_meta
from ...ingestion.sidecar import load_sidecar, merge_doc_meta
from ...ingestion.snapshot import delete_snapshot, load_all_snapshots, save_snapshot
from ...ingestion.splitter import split_pages
from ...providers import get_embedding_provider
from ...retrieval.vectorstore import get_vector_store
from ...text_utils import stable_hash


@safe_node("purge_stale", timeout=2.0)
def purge_stale(state: Dict[str, Any]) -> Dict[str, Any]:
    """文档下线清理：manifest 里有、磁盘上没有的文档，从向量库与快照中移除。"""
    manifest = read_manifest()
    removed = 0
    survivors: List[Dict[str, Any]] = []
    for d in manifest.get("docs", []):
        if Path(d.get("path", "")).exists():
            survivors.append(d)
        else:
            store = get_vector_store()
            removed += store.delete_by_doc(d["doc_id"])
            delete_snapshot(d["doc_id"])
    if removed or len(survivors) != len(manifest.get("docs", [])):
        manifest["docs"] = survivors
        write_manifest_atomic(manifest)
    return {"purge_stats": {"removed_docs": len(manifest.get("docs", [])) - len(survivors), "removed_chunks": removed}}


@safe_node("load_pdf", timeout=30.0)
def load_pdf_node(state: Dict[str, Any]) -> Dict[str, Any]:
    path = state.get("doc_path") or str(CONFIG.default_pdf)
    pages = load_pdf(path)
    fp = file_fingerprint(path)
    doc_id = pages[0]["doc_id"]
    return {
        "doc_path": path,
        "pages": pages,
        "doc_id": doc_id,
        "doc_meta": {"path": path, **fp},
    }


@safe_node("clean_normalize", timeout=10.0)
def clean_normalize(state: Dict[str, Any]) -> Dict[str, Any]:
    pages = state.get("pages") or []
    return {"pages_clean": clean_pages(pages)}


@safe_node("enrich_metadata", timeout=5.0)
def enrich_metadata(state: Dict[str, Any]) -> Dict[str, Any]:
    """元数据富化：网页 sidecar 优先，PDF 首页正则兜底。

    sidecar 缺失时仍会入库（不阻断），但打 degraded 标记：
    多文档语料下缺元数据的文档无法被 meta_recall 精确命中，只能在日志里暴露出来。
    """
    pages = state.get("pages_clean") or state.get("pages") or []
    heads = [p.get("text", "") for p in pages[:3]]
    meta = dict(state.get("doc_meta") or {})
    path = meta.get("path") or state.get("doc_path") or ""
    sidecar = load_sidecar(path)
    meta.update(merge_doc_meta(sidecar, detect_doc_meta(heads)))
    meta["ingest_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    embedder = get_embedding_provider()
    meta["embedding_provider"] = embedder.name
    meta["embed_dim"] = int(getattr(embedder, "dim", 0) or 0)
    meta["doc_id"] = state.get("doc_id", "")
    patch: Dict[str, Any] = {"doc_meta": meta}
    if not sidecar:
        patch["degraded"] = ["meta_no_sidecar:%s" % Path(path).name]
    return patch


@safe_node("table_aware_split", timeout=20.0)
def table_aware_split(state: Dict[str, Any]) -> Dict[str, Any]:
    pages = state.get("pages_clean") or []
    meta = state.get("doc_meta") or {}
    version = state.get("doc_version") or next_version(meta.get("doc_id", "unknown"))
    chunks = split_pages(pages, meta, version)
    if not chunks:
        raise ValueError("切分后没有任何 chunk")
    return {"chunks": chunks, "doc_version": version}


@safe_node("dedup_hash", timeout=5.0)
def dedup_hash(state: Dict[str, Any]) -> Dict[str, Any]:
    pages = state.get("pages_clean") or []
    meta = state.get("doc_meta") or {}
    page_hashes = [p.get("page_hash") or stable_hash(p.get("text", "")) for p in pages]
    return {
        "page_hashes": page_hashes,
        "doc_hash": meta.get("file_sha256", ""),
    }


@safe_node("plan_diff", timeout=2.0)
def plan_diff_node(state: Dict[str, Any]) -> Dict[str, Any]:
    meta = state.get("doc_meta") or {}
    doc_id = meta.get("doc_id", "")
    manifest = read_manifest()
    embedder = get_embedding_provider()
    plan = plan_diff(
        manifest,
        doc_id,
        meta.get("file_sha256", ""),
        state.get("page_hashes") or [],
        embedder.name,
        int(getattr(embedder, "dim", 0) or 0),
        force_rebuild=bool(state.get("force_rebuild")),
    )
    if plan["mode"] != "skip":
        plan["doc_version"] = next_version(doc_id)
    return {"diff_plan": plan, "manifest": manifest}


@safe_node("skip_node", timeout=1.0)
def skip_node(state: Dict[str, Any]) -> Dict[str, Any]:
    return {"skipped": True, "upsert_stats": {"skipped": True, "reason": (state.get("diff_plan") or {}).get("reason", "")}}


@safe_node("embed_upsert", timeout=120.0, provider="embedding")
def embed_upsert(state: Dict[str, Any]) -> Dict[str, Any]:
    chunks: List[Dict[str, Any]] = list(state.get("chunks") or [])
    meta = state.get("doc_meta") or {}
    doc_id = meta.get("doc_id", "")
    plan = state.get("diff_plan") or {}
    version = plan.get("doc_version") or state.get("doc_version") or next_version(doc_id)

    store = get_vector_store()

    # 删除旧数据：page 级增量优先，失败则整文档替换
    changed = plan.get("changed_pages") or []
    if plan.get("mode") == "page" and changed:
        try:
            store.delete_where({"doc_id": doc_id, "page": {"$in": list(changed)}})
        except Exception:
            store.delete_by_doc(doc_id)
    else:
        store.delete_by_doc(doc_id)

    embedder = get_embedding_provider()
    texts = [c.get("text", "") for c in chunks]
    vectors = embedder.embed_documents(texts)

    for c in chunks:
        c["doc_version"] = version
        c["embedding_provider"] = embedder.name
        c["embed_dim"] = int(getattr(embedder, "dim", 0) or 0)
    written = store.upsert(chunks, vectors)
    save_snapshot(doc_id, chunks)

    return {
        "doc_version": version,
        "upsert_stats": {
            "written": written,
            "mode": plan.get("mode", "full"),
            "embedding_provider": embedder.name,
            "embed_dim": int(getattr(embedder, "dim", 0) or 0),
        },
    }


@safe_node("build_bm25", timeout=20.0)
def build_bm25(state: Dict[str, Any]) -> Dict[str, Any]:
    """重建 BM25 倒排。

    skip_bm25=True 时只打标不重建：批量入库场景下每份文档都全量重建是 O(n²)
    （每份都要加载此前所有文档快照），由批量脚本在末尾调用 rebuild_bm25() 统一重建一次。
    """
    manifest = read_manifest()
    meta = state.get("doc_meta") or {}
    doc_id = meta.get("doc_id", "")
    ids = active_doc_ids(manifest)
    if doc_id and doc_id not in ids:
        ids = ids + [doc_id]
    if state.get("skip_bm25"):
        return {"bm25_stats": {"docs": len(ids), "chunks": 0, "deferred": True}}
    chunks = load_all_snapshots(ids)
    idx = BM25Index(chunks)
    idx.save()
    return {"bm25_stats": {"docs": len(ids), "chunks": len(chunks), "deferred": False}}


@safe_node("write_manifest", timeout=5.0)
def write_manifest(state: Dict[str, Any]) -> Dict[str, Any]:
    meta = dict(state.get("doc_meta") or {})
    doc_id = meta.get("doc_id", "")
    plan = state.get("diff_plan") or {}
    stats = state.get("upsert_stats") or {}
    embedder = get_embedding_provider()

    manifest = read_manifest()
    old = get_doc(manifest, doc_id) or {}
    entry = {
        "doc_id": doc_id,
        "path": meta.get("path", ""),
        "size": meta.get("size", 0),
        "mtime": meta.get("mtime", 0),
        "file_sha256": meta.get("file_sha256", ""),
        "page_count": len(state.get("pages_clean") or []),
        "chunk_count": int(stats.get("written", 0) or 0),
        "doc_version": plan.get("doc_version") or state.get("doc_version") or old.get("doc_version", ""),
        "page_hashes": state.get("page_hashes") or [],
        "embedding_provider": embedder.name,
        "embed_dim": int(getattr(embedder, "dim", 0) or 0),
        "company": meta.get("company", ""),
        "short_name": meta.get("short_name", ""),
        "stock_code": meta.get("stock_code", ""),
        "report_period": meta.get("report_period", ""),
        "report_type": meta.get("report_type", ""),
        "industry": meta.get("industry", ""),
        "source_url": meta.get("source_url", ""),
        "meta_source": meta.get("meta_source", ""),
        "ingest_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prev_version": old.get("doc_version", "") or plan.get("old_version", ""),
        "status": "active",
    }
    # 计数校验：写 manifest 前确认库内确有数据，避免"写一半"的脏索引被在线读到
    store = get_vector_store()
    if store.count() <= 0:
        raise ValueError("向量库为空，拒绝写入 manifest（保留旧版本）")
    upsert_doc(manifest, entry)
    write_manifest_atomic(manifest)
    return {"manifest": manifest, "doc_version": entry["doc_version"]}
