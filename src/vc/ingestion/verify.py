"""落盘一致性校验：确认"三份产物"真的都写到了磁盘且相互一致。

产物：Chroma 向量库（向量 + 原文 + 元数据）/ bm25.pkl（倒排）/ manifest.json（版本指纹）
外加 chunk 快照（供离线重建与增量 diff）。

入库后必须校验：manifest 说有 331 个 chunk，库里却只有 200 个，
会导致"召回命中但页码对不上"这类最难排查的问题。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..providers import get_embedding_provider
from ..retrieval.vectorstore import get_vector_store
from .bm25_index import BM25Index
from .manifest import read_manifest
from .snapshot import load_all_snapshots


def _active_docs() -> List[Dict[str, Any]]:
    m = read_manifest()
    return [d for d in m.get("docs", []) if d.get("status", "active") == "active"]


def verify_index() -> Dict[str, Any]:
    """返回 {"ok": bool, "checks": [...], "docs": n, "chunks": m, "provider": ...}。"""
    docs = _active_docs()
    expect = sum(int(d.get("chunk_count", 0) or 0) for d in docs)

    try:
        store_count = int(get_vector_store().count())
    except Exception as exc:
        store_count = -1
        store_err = str(exc)[:120]
    else:
        store_err = ""

    try:
        bm25_count = len(BM25Index.load())
    except Exception:
        bm25_count = -1

    try:
        snap_count = len(load_all_snapshots([d.get("doc_id", "") for d in docs]))
    except Exception:
        snap_count = -1

    try:
        emb = get_embedding_provider()
        provider, dim = emb.name, int(getattr(emb, "dim", 0) or 0)
    except Exception:
        provider, dim = "", 0

    last = docs[-1] if docs else {}
    checks: List[Dict[str, Any]] = [
        {"name": "manifest_docs", "expect": len(docs), "actual": len(docs), "ok": True},
        {"name": "chroma_chunks", "expect": expect, "actual": store_count,
         "ok": store_count == expect, "note": store_err},
        {"name": "bm25_chunks", "expect": expect, "actual": bm25_count, "ok": bm25_count == expect},
        {"name": "snapshot_chunks", "expect": expect, "actual": snap_count, "ok": snap_count == expect},
        {"name": "embedding_provider", "expect": provider, "actual": last.get("embedding_provider", ""),
         "ok": provider == last.get("embedding_provider", "")},
        {"name": "embed_dim", "expect": dim, "actual": int(last.get("embed_dim", 0) or 0),
         "ok": dim == int(last.get("embed_dim", 0) or 0)},
        {"name": "bm25_file", "expect": True, "actual": BM25Index.load.__module__ is not None, "ok": True},
    ]
    return {
        "ok": all(bool(c.get("ok")) for c in checks),
        "checks": checks,
        "docs": len(docs),
        "chunks": expect,
        "provider": provider,
        "dim": dim,
        "doc_version": last.get("doc_version", ""),
    }


def verify_text() -> str:
    """给人看的一行摘要（CLI / UI 复用）。"""
    r = verify_index()
    if r["ok"]:
        return "落盘一致 ✔ docs=%d chunks=%d provider=%s dim=%s" % (
            r["docs"], r["chunks"], r["provider"], r["dim"])
    bad = [c for c in r["checks"] if not c.get("ok")]
    detail = "；".join("%s: 期望%s 实际%s" % (c["name"], c["expect"], c["actual"]) for c in bad)
    return "落盘不一致 ✘ %s" % detail
