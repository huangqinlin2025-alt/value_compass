"""知识更新核心：manifest 索引 / 增量决策 / 原子替换 / 回滚。

manifest.json 结构：
{
  "version": 1,
  "updated_at": 1690000000.0,
  "docs": [
    {"doc_id": "...", "path": "...", "size": 123, "mtime": 169, "file_sha256": "...",
     "page_count": 118, "chunk_count": 512, "doc_version": "ab12@202609181200",
     "page_hashes": ["..."], "embedding_provider": "hashing", "embed_dim": 384,
     "company": "...", "stock_code": "600007", "report_period": "2026H1", "status": "active"}
  ]
}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import CONFIG

EMPTY_MANIFEST: Dict[str, Any] = {"version": 1, "updated_at": 0.0, "docs": []}


def read_manifest(path: str = None) -> Dict[str, Any]:
    p = Path(path or CONFIG.manifest_path)
    if not p.exists():
        return dict(EMPTY_MANIFEST)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # manifest 损坏时不能让在线服务挂掉，按空清单处理并触发全量重建
        return dict(EMPTY_MANIFEST)


def write_manifest_atomic(manifest: Dict[str, Any], path: str = None) -> None:
    """原子写：先写 .tmp 再 os.replace，避免"写一半"的脏 manifest。"""
    p = Path(path or CONFIG.manifest_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    manifest = dict(manifest)
    manifest["updated_at"] = time.time()
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def get_doc(manifest: Dict[str, Any], doc_id: str) -> Optional[Dict[str, Any]]:
    for d in manifest.get("docs", []):
        if d.get("doc_id") == doc_id:
            return d
    return None


def upsert_doc(manifest: Dict[str, Any], doc: Dict[str, Any]) -> Dict[str, Any]:
    docs = [d for d in manifest.get("docs", []) if d.get("doc_id") != doc.get("doc_id")]
    docs.append(doc)
    manifest["docs"] = docs
    return manifest


def next_version(doc_id: str) -> str:
    return "%s@%s" % (doc_id, time.strftime("%Y%m%d%H%M"))


def plan_diff(
    manifest: Dict[str, Any],
    doc_id: str,
    file_sha256: str,
    page_hashes: List[str],
    embedding_provider: str,
    embed_dim: int,
    force_rebuild: bool = False,
) -> Dict[str, Any]:
    """增量决策树：
    - 强制重建 / 无历史 / provider 或 dim 变化 -> full
    - file_sha256 未变 -> skip
    - 文件变化 -> 逐页 diff -> page（只重嵌变更页）
    """
    old = get_doc(manifest, doc_id)
    if force_rebuild or not old:
        return {"mode": "full", "reason": "force_rebuild" if force_rebuild else "new_doc",
                "changed_pages": [], "old_version": (old or {}).get("doc_version", "")}
    if old.get("embedding_provider") != embedding_provider or int(old.get("embed_dim", 0)) != int(embed_dim):
        return {"mode": "full", "reason": "embedding_changed", "changed_pages": [],
                "old_version": old.get("doc_version", "")}
    if old.get("file_sha256") == file_sha256 and old.get("status") == "active":
        return {"mode": "skip", "reason": "unchanged", "changed_pages": [],
                "old_version": old.get("doc_version", "")}
    old_pages = list(old.get("page_hashes", []))
    changed = [i + 1 for i in range(max(len(old_pages), len(page_hashes)))
               if (old_pages[i] if i < len(old_pages) else None) != (page_hashes[i] if i < len(page_hashes) else None)]
    if len(changed) > max(5, int(len(page_hashes) * 0.5)):
        return {"mode": "full", "reason": "pages_changed_%d" % len(changed), "changed_pages": changed,
                "old_version": old.get("doc_version", "")}
    return {"mode": "page", "reason": "pages_changed_%d" % len(changed), "changed_pages": changed,
            "old_version": old.get("doc_version", "")}


def mark_stale(manifest: Dict[str, Any], doc_id: str) -> Dict[str, Any]:
    for d in manifest.get("docs", []):
        if d.get("doc_id") == doc_id:
            d["status"] = "stale"
    return manifest


def active_doc_ids(manifest: Dict[str, Any]) -> List[str]:
    return [d["doc_id"] for d in manifest.get("docs", []) if d.get("status", "active") == "active"]
