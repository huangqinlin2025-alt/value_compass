"""可观测性：trace_id + 结构化 JSON 日志 + 节点耗时埋点。

安全要求：日志禁止打印密钥、请求头与正文全文，只记录摘要（长度/页码/错误码）。
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional

_VC_LOGGER_NAME = "vc"


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(_VC_LOGGER_NAME)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def log_event(event: str, **fields: Any) -> None:
    logger = setup_logging()
    payload = {"event": event, "ts": round(time.time(), 3)}
    payload.update(fields)
    try:
        logger.info(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:  # 日志绝不能影响主流程
        pass


def trace_step(
    node: str,
    latency_ms: float,
    ok: bool,
    error_code: str = "",
    degraded: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    """生成一条可累加进 state["trace"] 的记录。"""
    item = {
        "node": node,
        "latency_ms": round(latency_ms, 2),
        "ok": ok,
        "error_code": error_code,
        "degraded": degraded,
        "ts": round(time.time(), 3),
    }
    item.update(extra)
    log_event("node", **item)
    return item


@contextmanager
def timer():
    start = time.time()
    holder = {"ms": 0.0}

    def _elapsed() -> float:
        return (time.time() - start) * 1000.0

    try:
        yield _elapsed
    finally:
        holder["ms"] = _elapsed()


def summarize_text(text: str, limit: int = 80) -> str:
    """日志中使用的安全摘要，避免打印全文。"""
    if not text:
        return ""
    return (text[:limit] + "…") if len(text) > limit else text


def safe_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not meta:
        return {}
    return {k: (v if not isinstance(v, str) or len(v) < 60 else v[:60] + "…") for k, v in meta.items()}
