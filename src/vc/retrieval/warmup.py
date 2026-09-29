"""大语料冷启动预热。

实测（10 家公司 60 份财报 ≈ 5.7 万 chunk）：
- 加载 `bm25.pkl` 倒排 ≈ 7s
- embedding provider（BGE）+ 向量库初始化 ≈ 3s

这笔**一次性**开销如果压在首个召回节点里，会被 `timeout_bm25=1s` / `timeout_vector=3s`
的预算掐掉，表现为三路召回同时 `E_TIMEOUT` 并整体转兜底——看起来像"检索坏了"，
实际只是第一次查询在付冷启动的钱。预热把它挪到节点计时之外，之后每次召回回到
BM25 ≈0.3s / 向量 ≈0.05s 的热态水平。
"""
from __future__ import annotations

import time
from typing import Any, Dict

_WARMED = False


def warmup_index(force: bool = False) -> Dict[str, Any]:
    """把倒排 / 向量库 / embedding 模型提前加载进进程内缓存。

    幂等且**失败不抛**：预热只是性能优化，任何一项失败都应由真正用到它的节点去降级。
    """
    global _WARMED
    if _WARMED and not force:
        return {"warmed": False, "reason": "already"}
    stats: Dict[str, Any] = {"warmed": True}
    t0 = time.time()

    try:
        from ..ingestion.bm25_index import get_bm25

        # 注意：这里是 refresh=，不是 force=（参数名写错会被下面的 except 吞掉，等于没预热）
        idx = get_bm25(refresh=force or not _WARMED)
        stats["bm25_ms"] = int((time.time() - t0) * 1000)
        stats["bm25_chunks"] = len(getattr(idx, "chunks", []) or [])
    except Exception as exc:
        stats["bm25_error"] = "%s: %s" % (type(exc).__name__, exc)

    t1 = time.time()
    try:
        from ..providers import get_embedding_provider

        get_embedding_provider()
        stats["embed_ms"] = int((time.time() - t1) * 1000)
    except Exception as exc:
        stats["embed_error"] = "%s: %s" % (type(exc).__name__, exc)

    t2 = time.time()
    try:
        from .vectorstore import get_vector_store

        store = get_vector_store()
        stats["store_ms"] = int((time.time() - t2) * 1000)
        stats["store_chunks"] = store.count()
    except Exception as exc:
        stats["store_error"] = "%s: %s" % (type(exc).__name__, exc)

    stats["total_ms"] = int((time.time() - t0) * 1000)
    _WARMED = True
    try:
        from ..observability import log_event

        stats["event"] = "warmup"
        log_event("index_warmup", **stats)
    except Exception:
        pass
    return stats
