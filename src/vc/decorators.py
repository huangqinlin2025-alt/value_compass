"""safe_node 装饰器：节点级超时 / 重试 / 异常归一化 / 降级标记 / trace 埋点。

设计要点（容错隔离的核心）：
- 节点**永不向外抛异常**，失败时返回"安全的最小可用 patch"，由条件边决定降级还是兜底。
- 错误只写入 state["errors"]（add_list 累加），主字段失败时置空，不污染。
- 支持 provider 维度熔断：连续失败达阈值后本次进程内直接短路。
"""
from __future__ import annotations

import functools
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable, Dict, Optional

from .config import CONFIG
from .errors import (
    CIRCUIT,
    ErrorCode,
    TimeoutVCException,
    VCException,
    from_exception,
    is_retryable,
    make_error,
)
from .observability import trace_step

_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="vc-node")


def _invoke(fn: Callable, state: Dict[str, Any], timeout: Optional[float], name: str) -> Dict[str, Any]:
    if not timeout:
        return fn(state)
    fut = _EXECUTOR.submit(fn, state)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout:
        raise TimeoutVCException(name, "节点 %s 超过 %.1fs 未返回" % (name, timeout))


def safe_node(
    name: str,
    timeout: Optional[float] = None,
    retries: Optional[int] = None,
    provider: Optional[str] = None,
    fallback_patch: Optional[Dict[str, Any]] = None,
):
    """包裹一个 (state) -> dict 的节点函数。"""

    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrapper(state: Dict[str, Any]) -> Dict[str, Any]:
            start = time.time()

            # 1) 熔断短路
            if provider and CIRCUIT.is_open(provider):
                err = make_error(
                    ErrorCode.E_INTERNAL, name,
                    "provider=%s 处于熔断状态，已跳过" % provider,
                    fallback="%s_skipped" % name,
                )
                patch = dict(fallback_patch or {})
                patch.setdefault("degraded", [])
                patch["errors"] = list(patch.get("errors") or []) + [err]
                patch["degraded"] = list(patch.get("degraded") or []) + ["%s:circuit_open" % name]
                patch["trace"] = [trace_step(name, (time.time() - start) * 1000, False, err["code"], "circuit_open")]
                return patch

            max_retry = CONFIG.max_retry if retries is None else retries
            attempt = 0
            while True:
                try:
                    patch = _invoke(fn, state, timeout, name)
                    if patch is None:
                        patch = {}
                    if not isinstance(patch, dict):
                        raise VCException(ErrorCode.E_INTERNAL, "节点 %s 返回值必须是 dict" % name)
                    if provider:
                        CIRCUIT.record_success(provider)
                    patch["trace"] = list(patch.get("trace") or []) + [
                        trace_step(name, (time.time() - start) * 1000, True)
                    ]
                    return patch

                except Exception as exc:  # noqa: BLE001 —— 统一收敛，避免异常炸图
                    err = from_exception(exc, name, fallback="%s_fallback" % name)
                    attempt += 1
                    if is_retryable(err) and attempt <= max_retry:
                        time.sleep(0.3 * (3 ** (attempt - 1)))  # 0.3s -> 0.9s
                        continue
                    if provider:
                        CIRCUIT.record_fail(provider)

                    patch = dict(fallback_patch or {})
                    patch["errors"] = list(patch.get("errors") or []) + [err]
                    patch["degraded"] = list(patch.get("degraded") or []) + [name]
                    patch["trace"] = list(patch.get("trace") or []) + [
                        trace_step(name, (time.time() - start) * 1000, False, err["code"], "%s_fallback" % name)
                    ]
                    return patch

        wrapper.vc_node_name = name  # 便于测试与文档生成
        return wrapper

    return deco
