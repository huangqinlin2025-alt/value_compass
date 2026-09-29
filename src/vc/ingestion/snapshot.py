"""chunk 快照：BM25 与向量库的"同源真值"。

两路召回必须基于同一份 chunk 快照，否则同一 chunk_id 在两路内容不一致，
会导致引用页码错位。快照按 doc_id 落盘，供多文档增量重建 BM25 时使用。
"""
from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any, Dict, List

from ..config import CONFIG

SNAPSHOT_DIR = CONFIG.index_dir / "snapshots"


def _dir() -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return SNAPSHOT_DIR


def save_snapshot(doc_id: str, chunks: List[Dict[str, Any]]) -> None:
    p = _dir() / ("%s.pkl" % doc_id)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(list(chunks), f)
    tmp.replace(p)


def load_snapshot(doc_id: str) -> List[Dict[str, Any]]:
    p = _dir() / ("%s.pkl" % doc_id)
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return []


def load_all_snapshots(active_doc_ids: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for doc_id in active_doc_ids:
        out.extend(load_snapshot(doc_id))
    return out


def delete_snapshot(doc_id: str) -> bool:
    p = _dir() / ("%s.pkl" % doc_id)
    if p.exists():
        p.unlink()
        return True
    return False
