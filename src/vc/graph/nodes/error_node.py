"""error_node：错误的终点汇总（只汇总，不覆盖已有答案）。

用途：
- 当整条链路跑完仍拿不到 final_answer 时，给出可诊断但安全的回复；
- 把 errors / degraded / trace 汇总成 error_summary，供 /trace 与日志排查。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...config import CONFIG
from ...decorators import safe_node


@safe_node("error_node", timeout=0.5)
def error_node(state: Dict[str, Any]) -> Dict[str, Any]:
    errors = list(state.get("errors") or [])
    degraded = list(state.get("degraded") or [])
    trace = list(state.get("trace") or [])

    summary = {
        "trace_id": state.get("trace_id", ""),
        "error_count": len(errors),
        "codes": sorted({e.get("code", "") for e in errors if e.get("code")}),
        "degraded": sorted(set(degraded)),
        "nodes_executed": [t.get("node") for t in trace],
    }
    patch: Dict[str, Any] = {"error_summary": summary}
    if not (state.get("final_answer") or "").strip():
        patch["final_answer"] = (
            "抱歉，本次问答未能产出结果（trace_id=%s，错误码：%s）。"
            "请稍后重试或换一种问法。\n\n%s"
            % (summary["trace_id"] or "-", ",".join(summary["codes"]) or "无", CONFIG.disclaimer)
        )
    return patch
