"""clarify_node：意图不明时优先追问，而不是硬答。

富交互补充：除文本追问外，额外产出**结构化按钮**（ui_payload.options）。
前端渲染成可点卡片 -> 下一次 invoke 带 ui_action=clarify_followup + ui_filters，
由 query_rewrite / intent_router 双短路直接激活 Send 并行召回，
不必再让用户输入自然语言（也就不必依赖 query_raw）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...decorators import safe_node
from ...knowledge import SUGGESTIONS
from ...ui import STAR_NODES, UI_ACTION_FOLLOWUP, clean_ui_filters, label_for_node
from .router import classify

# 意图 -> 最可能被问的星图节点（作为澄清按钮候选）
_INTENT_NODES: Dict[str, List[str]] = {
    "METRIC": ["revenue", "net_profit", "gross_margin"],
    "CALC": ["gross_margin", "roe"],
    "COMPARE": ["revenue", "net_profit"],
    "TABLE": ["business_mix"],
    "SUMMARY": ["revenue", "risk"],
    "QUALITATIVE": ["risk", "strategy"],
}


def _pick_nodes(scores: Dict[str, float]) -> List[str]:
    """按意图打分挑出候选节点：得分高的意图对应的节点排在前面。"""
    out: List[str] = []
    for intent in sorted(scores or {}, key=lambda k: -scores[k]):
        for n in _INTENT_NODES.get(intent, []):
            if n not in out:
                out.append(n)
    return out or ["revenue", "net_profit"]


@safe_node("clarify_node", timeout=0.5)
def clarify_node(state: Dict[str, Any]) -> Dict[str, Any]:
    q = state.get("query_raw") or ""
    scores = classify(q)
    cands: List[str] = []
    if scores.get("METRIC"):
        cands.append("你想查的是哪个指标？例如：营业收入 / 净利润 / 资产负债率。")
    if scores.get("COMPARE"):
        cands.append("你想对比哪个期间？例如：2026年上半年 vs 2025年上半年。")
    if scores.get("TABLE"):
        cands.append("你想看哪张表的明细？例如：主营业务构成 / 现金流量表。")
    if not cands:
        cands.append("能补充一下具体的指标或期间吗？")

    body = "这个问题我还不够确定（置信度 %.2f）。%s\n\n也可以直接问我：\n%s" % (
        float(state.get("intent_confidence") or 0.0),
        cands[0],
        "\n".join("- " + s for s in SUGGESTIONS[:3]),
    )

    # 结构化按钮：继承已有的公司/期间约束，用户点一下即可补齐最后一个维度
    carried = {k: v for k, v in clean_ui_filters(state.get("ui_filters") or {}).items()
               if k in ("company", "stock_code", "report_period")}
    options = [
        {
            "node_id": nid,
            "label": label_for_node(nid),
            "ui_action": UI_ACTION_FOLLOWUP,
            "ui_filters": dict({"node_id": nid}, **carried),
        }
        for nid in _pick_nodes(scores)[:3]
    ]
    return {
        "final_answer": body,
        "suggestions": SUGGESTIONS[:3],
        "fallback_reason": "clarify",
        "ui_payload": {"type": "clarify", "prompt": body, "options": options},
    }
