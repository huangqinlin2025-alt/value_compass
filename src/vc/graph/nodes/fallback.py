"""兜底策略（五类场景）+ 越界拒答 + 闲聊直答。

统一原则：**绝不静默失败，也绝不在无来源时给出数字断言**。
每个兜底输出都必须带：原因说明 + 可继续的方向（建议/摘录/澄清）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...config import CONFIG
from ...decorators import safe_node
from ...knowledge import SUGGESTIONS, company_options, report_title
from ...text_utils import clean_display, clip
from ...ui import (
    UI_ACTION_FOLLOWUP,
    UI_ACTION_STAR,
    label_for_node,
    next_candidates,
    scope_of_chunks,
)

REASON_LABELS = {
    "empty": "未在报告中检索到相关内容",
    "low_score": "检索置信度偏低",
    "num_mismatch": "数值未通过原文一致性校验",
    "generate_failed": "生成模型暂不可用",
    "internal": "系统内部错误",
    "need_company": "未指定公司",
}

_CODE_TO_REASON = {
    "E_EMPTY_RECALL": "empty",
    "E_LOW_SCORE": "low_score",
    "E_NUM_MISMATCH": "num_mismatch",
    "E_CITATION_MISS": "num_mismatch",
    "E_LLM_TIMEOUT": "generate_failed",
    "E_LLM_RATE_LIMIT": "generate_failed",
    "E_LLM_BADJSON": "generate_failed",
    "E_INTERNAL": "internal",
    "E_TIMEOUT": "internal",
}


def _detect_reason(state: Dict[str, Any]) -> str:
    codes = [e.get("code") for e in (state.get("errors") or [])]
    for c in codes:
        if c in _CODE_TO_REASON:
            return _CODE_TO_REASON[c]
    return "internal"


def _need_company_tip() -> str:
    """未指定公司时的兜底话术：把可选项直接列出来，让用户一步到位。

    刻意不含任何数字（连"共 N 家"都不写）：兜底话术要过数值一致性闸门，
    文案里的数字会被判成"答案数字不在原文中"的假阳性。
    """
    opts = company_options()
    names = [o.get("short_name") or o.get("company") or o.get("stock_code") or "" for o in opts]
    names = [n for n in names if n]
    shown = "、".join(names[:6])
    tail = " 等" if len(names) > 6 else ""
    return "当前未指定公司，可跨公司查看该科目的通用口径；知识库包含：%s%s。选定公司后可查看该公司的实例数据。" % (shown, tail)


def _excerpts(state: Dict[str, Any], n: int = 3) -> str:
    pool = list(state.get("context") or state.get("reranked") or state.get("fused") or [])
    if not pool:
        return ""
    lines = []
    for c in pool[:n]:
        lines.append("- [P%s] %s" % (c.get("page", "?"), clip(clean_display(c.get("text", "")), 180)))
    return "\n".join(lines)


@safe_node("fallback_node", timeout=1.0, fallback_patch={"final_answer": "抱歉，暂时无法回答该问题。"})
def fallback_node(state: Dict[str, Any]) -> Dict[str, Any]:
    # 星图点击未锁定公司（库内多家且会话无公司）：这不是"没检索到"，
    # 而是"没法确定该答谁"，话术必须指向下一步动作——选公司。
    need_company = "ui:need_company" in (state.get("degraded") or [])
    reason = "need_company" if need_company else _detect_reason(state)

    if need_company:
        parts: List[str] = [_need_company_tip()]
    else:
        title = report_title()
        head = "未在%s中检索到可靠依据（%s）。" % (title, REASON_LABELS.get(reason, reason))
        parts = [head]
        if reason in ("low_score", "num_mismatch", "generate_failed"):
            ex = _excerpts(state)
            if ex:
                parts.append("以下为最相关的报告原文摘录，请核对：\n" + ex)
        parts.append("你可以换一种问法，例如：\n" + "\n".join("- " + s for s in SUGGESTIONS[:3]))
        parts.append(CONFIG.disclaimer)

    patch: Dict[str, Any] = {"final_answer": "\n\n".join(parts), "fallback_reason": reason}
    # 星图点击走兜底时也必须回一个可渲染的退化卡片，否则前端拿不到载荷会空屏
    if state.get("ui_action") in (UI_ACTION_STAR, UI_ACTION_FOLLOWUP):
        node_id = state.get("star_node_id") or ""
        pool = list(state.get("context") or state.get("reranked") or state.get("fused") or [])
        pages = sorted({int(c["page"]) for c in pool if c.get("page") is not None})[:3]
        patch["ui_payload"] = {
            "node_id": node_id,
            "label": label_for_node(node_id),
            "explanation": _need_company_tip() if need_company else head,
            "citations": ["P%d" % p for p in pages],
            "unlock_next": next_candidates(node_id, state.get("unlocked_nodes") or []),
            "refused": True,
            "degraded_reason": reason,
            "scope": scope_of_chunks(pool),
        }
    return patch


@safe_node("refuse_node", timeout=0.5)
def refuse_node(state: Dict[str, Any]) -> Dict[str, Any]:
    msg = (
        "抱歉，作为价值投资助手，我不提供个股买卖推荐、目标价或涨跌预测。"
        "我们可以一起看懂财报：例如营业收入、净利润、现金流、资产负债率等指标的口径与变化。\n\n"
        + CONFIG.disclaimer
    )
    return {"final_answer": msg, "fallback_reason": "oos"}


@safe_node("direct_answer", timeout=0.5)
def direct_answer(state: Dict[str, Any]) -> Dict[str, Any]:
    msg = (
        "你好，我是 value_compass 财报问答助手，可以基于已入库的财报回答：\n"
        "· 指标查询（营业收入、净利润、资产负债率…）\n"
        "· 同比/环比对比与构成明细\n"
        "· 章节摘要与原文定位\n\n"
        "试试问我：" + SUGGESTIONS[0] + "\n\n" + CONFIG.disclaimer
    )
    return {"final_answer": msg, "fallback_reason": "chitchat"}
