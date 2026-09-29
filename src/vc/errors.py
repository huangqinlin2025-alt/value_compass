"""统一错误体系。

设计要点：
1. 所有错误收敛为 ErrorCode 枚举，禁止节点自定义裸字符串。
2. 错误只**累加**进 state["errors"]，绝不覆盖 answer/context 等主字段。
3. 每个错误必须声明 retryable 与 severity，供 safe_node 决定重试还是降级。
4. 熔断按 provider 维度统计，避免故障 provider 反复拖慢整条链路。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .config import CONFIG


class ErrorCode(Enum):
    # 入库侧
    E_LOAD = "E_LOAD"
    E_SPLIT = "E_SPLIT"
    E_EMBED = "E_EMBED"
    E_STORE_UPSERT = "E_STORE_UPSERT"
    E_MANIFEST_WRITE = "E_MANIFEST_WRITE"
    # 召回侧
    E_BM25 = "E_BM25"
    E_STORE_QUERY = "E_STORE_QUERY"
    E_META_FILTER = "E_META_FILTER"
    E_EMPTY_RECALL = "E_EMPTY_RECALL"
    E_LOW_SCORE = "E_LOW_SCORE"
    # 生成侧
    E_RERANK = "E_RERANK"
    E_LLM_TIMEOUT = "E_LLM_TIMEOUT"
    E_LLM_RATE_LIMIT = "E_LLM_RATE_LIMIT"
    E_LLM_BADJSON = "E_LLM_BADJSON"
    E_NUM_MISMATCH = "E_NUM_MISMATCH"
    E_CITATION_MISS = "E_CITATION_MISS"
    # 路由与兜底
    E_INTENT_UNCLEAR = "E_INTENT_UNCLEAR"
    E_OOS = "E_OOS"
    E_TIMEOUT = "E_TIMEOUT"
    E_INTERNAL = "E_INTERNAL"


# 可重试错误（网络抖动、限流、超时）
RETRYABLE = {
    ErrorCode.E_LLM_TIMEOUT,
    ErrorCode.E_LLM_RATE_LIMIT,
    ErrorCode.E_TIMEOUT,
    ErrorCode.E_STORE_QUERY,
    ErrorCode.E_EMBED,
}

# 严重级别
SEVERITY = {
    ErrorCode.E_LOAD: "fatal",
    ErrorCode.E_SPLIT: "error",
    ErrorCode.E_EMBED: "error",
    ErrorCode.E_STORE_UPSERT: "fatal",
    ErrorCode.E_MANIFEST_WRITE: "error",
    ErrorCode.E_BM25: "warn",
    ErrorCode.E_STORE_QUERY: "warn",
    ErrorCode.E_META_FILTER: "warn",
    ErrorCode.E_EMPTY_RECALL: "warn",
    ErrorCode.E_LOW_SCORE: "warn",
    ErrorCode.E_RERANK: "warn",
    ErrorCode.E_LLM_TIMEOUT: "error",
    ErrorCode.E_LLM_RATE_LIMIT: "error",
    ErrorCode.E_LLM_BADJSON: "error",
    ErrorCode.E_NUM_MISMATCH: "error",
    ErrorCode.E_CITATION_MISS: "warn",
    ErrorCode.E_INTENT_UNCLEAR: "warn",
    ErrorCode.E_OOS: "warn",
    ErrorCode.E_TIMEOUT: "warn",
    ErrorCode.E_INTERNAL: "error",
}


class VCException(Exception):
    """业务异常基类，携带 ErrorCode。"""

    def __init__(self, code: ErrorCode, message: str = "", cause: Optional[Exception] = None):
        self.code = code
        self.message = message or code.value
        self.cause = cause
        super().__init__(self.message)


class TimeoutVCException(VCException):
    def __init__(self, node: str = "", message: str = ""):
        super().__init__(ErrorCode.E_TIMEOUT, message or ("节点超时: %s" % node))


@dataclass
class ErrorItem:
    code: str
    node: str
    message: str
    retryable: bool
    severity: str
    fallback: Optional[str] = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "code": self.code,
            "node": self.node,
            "message": self.message,
            "retryable": self.retryable,
            "severity": self.severity,
            "fallback": self.fallback,
            "ts": self.ts,
        }


def make_error(
    code: ErrorCode,
    node: str,
    message: str = "",
    fallback: Optional[str] = None,
) -> Dict:
    """构造一个可放入 state["errors"] 的 dict（保持 JSON 可序列化）。"""
    return ErrorItem(
        code=code.value,
        node=node,
        message=(message or code.value)[:500],
        retryable=code in RETRYABLE,
        severity=SEVERITY.get(code, "error"),
        fallback=fallback,
    ).to_dict()


def from_exception(exc: Exception, node: str, fallback: Optional[str] = None) -> Dict:
    """把任意异常归一化成 ErrorItem dict。"""
    if isinstance(exc, VCException):
        code = exc.code
        msg = exc.message
    else:
        code = ErrorCode.E_INTERNAL
        msg = "%s: %s" % (type(exc).__name__, exc)
    return make_error(code, node, msg, fallback)


def is_retryable(err: Dict) -> bool:
    return bool(err.get("retryable"))


class CircuitBreaker:
    """极简熔断器：同一 provider 连续失败 N 次后，在 TTL 内直接短路走降级。"""

    def __init__(self, threshold: int = None, ttl: float = None):
        self.threshold = threshold or CONFIG.circuit_threshold
        self.ttl = ttl or CONFIG.circuit_ttl
        self._fails: Dict[str, List[float]] = {}

    def _prune(self, key: str) -> None:
        now = time.time()
        self._fails[key] = [t for t in self._fails.get(key, []) if now - t < self.ttl]

    def is_open(self, key: str) -> bool:
        self._prune(key)
        return len(self._fails.get(key, [])) >= self.threshold

    def record_fail(self, key: str) -> None:
        self._prune(key)
        self._fails.setdefault(key, []).append(time.time())

    def record_success(self, key: str) -> None:
        self._fails.pop(key, None)

    def reset(self) -> None:
        self._fails.clear()


CIRCUIT = CircuitBreaker()
