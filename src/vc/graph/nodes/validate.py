"""citation_validate + output_guard。

citation_validate：引用必须能溯源到真实页码，编造的引用直接剥离并记错。
output_guard：合规收口 —— 无来源却出现数字一律转兜底，统一追加免责声明。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ...config import CONFIG
from ...decorators import safe_node
from ...errors import ErrorCode, make_error
from ...providers.llm import extract_citations
from ...text_utils import clean_display, clip
from ...ui import UI_ACTION_RESET

_HAS_NUM = re.compile(r"\d")


@safe_node("citation_validate", timeout=1.0, fallback_patch={"citations": [], "valid": False})
def citation_validate(state: Dict[str, Any]) -> Dict[str, Any]:
    answer = state.get("answer") or state.get("draft_answer") or ""
    ctx = state.get("context") or []
    ctx_pages = {int(c["page"]): c for c in ctx if c.get("page") is not None}
    cites = extract_citations(answer)
    bad = [p for p in cites if p not in ctx_pages]

    citations: List[Dict[str, Any]] = []
    for p in sorted(set(cites)):
        if p in ctx_pages:
            c = ctx_pages[p]
            citations.append({
                "page": p,
                "doc_id": c.get("doc_id", ""),
                "section_path": c.get("section_path", ""),
                "snippet": clip(clean_display(c.get("text", "")), 160),
            })

    patch: Dict[str, Any] = {"citations": citations}
    if bad:
        cleaned = re.sub(r"\[P\d+\]", "", answer)
        patch["answer"] = cleaned
        patch["valid"] = False
        patch["errors"] = [make_error(ErrorCode.E_CITATION_MISS, "citation_validate",
                                      "剥离了无法溯源的引用页码: %s" % bad[:5], "strip_citation")]
    elif not citations and _HAS_NUM.search(answer):
        patch["valid"] = False
        patch["errors"] = [make_error(ErrorCode.E_CITATION_MISS, "citation_validate",
                                      "答案含数字但无任何可溯源引用", "fallback")]
    else:
        patch["valid"] = True
    return patch


@safe_node("output_guard", timeout=1.0, fallback_patch={"final_answer": "抱歉，暂时无法回答该问题。"})
def output_guard(state: Dict[str, Any]) -> Dict[str, Any]:
    answer = (state.get("answer") or state.get("draft_answer") or "").strip()
    citations = state.get("citations") or []
    valid = state.get("valid")

    if not answer:
        # 纯拓扑动作（新建节点 / 连边 / 重置）自己产出了文案，链路里没有 answer，
        # 兜底成"未找到相关信息"会把"已新建节点"覆盖成一句答非所问的话。
        answer = str((state.get("ui_payload") or {}).get("hint") or "")
    if not answer:
        answer = "未在报告中找到相关信息。"
    elif valid is False and _HAS_NUM.search(answer) and not citations:
        # 无来源的数字断言是金融场景最危险的输出，直接拦掉
        answer = "未能从报告中获得可溯源的数值，以下为原文摘录：\n" + _excerpt(state)

    body = answer
    if citations:
        body += "\n\n来源：" + "、".join("P%s" % c["page"] for c in citations[:6])
    body += "\n\n" + CONFIG.disclaimer

    patch: Dict[str, Any] = {"final_answer": body}

    # 星图卡片必须与 final_answer 同口径：卡片渲染的是 ui_payload.explanation 而不是正文，
    # 若 guard 改写了答案（无源数字转摘录）而卡片不动，等于把未过闸门的原文直接推给用户。
    payload = dict(state.get("ui_payload") or {})
    # 纯 UI 动作（重置 / 连边 / 锁定引导）自己产出了文案，链路里根本没有 answer。
    # 若继续走下面的逻辑，answer 为空会兜底成"未在报告中找到相关信息"，
    # 把"已重置知识星图"覆盖掉 —— 用户点了重置却看到一句答非所问的话。
    if payload.get("bypass_guard"):
        payload["disclaimer"] = CONFIG.disclaimer
        return {"ui_payload": payload}
    # reset_starmap 不进检索：它的卡片由 unlock_commit 直接产出，
    # 若参与下面的引用同步，会被上一轮残留的 citations 覆盖成"重置卡片挂着旧页码"。
    if payload and state.get("ui_action") != UI_ACTION_RESET:
        if valid is False and _HAS_NUM.search(payload.get("explanation") or "") and not citations:
            payload["explanation"] = "未能从报告中获得可溯源的数值。"
            payload["citations"] = []
            payload["refused"] = True
        elif citations:
            # 用校验通过的页码覆盖，保证卡片角标与正文引用一致
            payload["citations"] = ["P%s" % c["page"] for c in citations[:6]]
        # 独立字段：不占 explanation 的 100 字预算，由前端在卡片底部渲染
        payload["disclaimer"] = CONFIG.disclaimer
        patch["ui_payload"] = payload
    return patch


def _excerpt(state: Dict[str, Any]) -> str:
    ctx = list(state.get("context") or [])[:3]
    if not ctx:
        return "（无可用片段）"
    return "\n".join("- [P%s] %s" % (c.get("page", "?"), clip(clean_display(c.get("text", "")), 160)) for c in ctx)
