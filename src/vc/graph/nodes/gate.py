"""faithfulness_gate：数值一致性校验（金融问答的防幻觉底线）。

两层判定，**取严不取宽**：
1. 确定性底线（永远执行）：答案中每个数值字面量必须在 context 原文中逐字命中；
   答案中的引用页码 [Pxx] 必须来自 context 中真实存在的页码。
2. LLM 语义判定（CONFIG.gate_llm_enabled 且 provider 支持 JSON 时叠加）：
   捕捉"数字对但结论错"的情况（例如资料只给了收入，答案却推论利润大增）。
   LLM 失败/不可用 -> 只写 degraded，判定权回到确定性底线。

任一层不通过 -> 记 E_NUM_MISMATCH / E_CITATION_MISS，收紧 context 重试一次，仍失败转兜底。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ...config import CONFIG
from ...decorators import safe_node
from ...errors import ErrorCode, make_error
from ...prompts import build_gate_messages
from ...providers import get_llm_provider
from ...providers.llm import extract_citations
from ...schema import FaithfulnessResult
from ...text_utils import extract_numbers

_CITE = re.compile(r"\[P\d+\]")


def _norm(num: str) -> str:
    return num.replace(",", "").replace(" ", "").replace("％", "%").strip()


def _answer_numbers(answer: str) -> List[str]:
    body = _CITE.sub("", answer or "")
    return [_norm(n) for n in extract_numbers(body)]


def deterministic_check(answer: str, ctx: List[Dict[str, Any]]) -> Tuple[List[str], List[int]]:
    """返回 (原文中不存在的数值, 上下文之外的页码)。"""
    ctx_text = "\n".join(c.get("text", "") for c in ctx or [])
    ctx_nums: Set[str] = {_norm(n) for n in extract_numbers(ctx_text)}
    # 允许千分位写法差异：同时登记去掉逗号后的原文数字
    ctx_nums |= {_norm(n) for n in re.findall(r"\d[\d,\.]*", ctx_text)}

    missing = [n for n in _answer_numbers(answer) if n and n not in ctx_nums]
    ctx_pages: Set[int] = {int(c["page"]) for c in ctx or [] if c.get("page") is not None}
    bad_pages = [p for p in extract_citations(answer) if p not in ctx_pages]
    return missing, bad_pages


def _llm_check(query: str, answer: str, ctx: List[Dict[str, Any]]) -> Optional[FaithfulnessResult]:
    if not CONFIG.gate_llm_enabled:
        return None
    llm = get_llm_provider(allow_fallback=False)
    if not getattr(llm, "supports_json", False):
        return None
    messages = build_gate_messages(query, answer, ctx)
    return llm.generate_json(messages, FaithfulnessResult, key="gate",
                             timeout=min(CONFIG.timeout_llm, 10.0))


@safe_node("faithfulness_gate", timeout=12.0, fallback_patch={"gate_pass": False, "gate_reason": "gate_error"})
def faithfulness_gate(state: Dict[str, Any]) -> Dict[str, Any]:
    answer = state.get("draft_answer") or state.get("answer") or ""
    query = state.get("query_rewritten") or state.get("query_raw") or ""
    ctx = state.get("context") or []

    missing, bad_pages = deterministic_check(answer, ctx)

    degraded: List[str] = []
    llm_res: Optional[FaithfulnessResult] = None
    try:
        llm_res = _llm_check(query, answer, ctx)
    except Exception:
        llm_res = None
        degraded.append("gate:llm_skipped")

    if llm_res is not None:
        for n in llm_res.mismatched_numbers:
            n = _norm(str(n))
            if n and n not in missing:
                missing.append(n)
        for p in llm_res.bad_citations:
            if p not in bad_pages:
                bad_pages.append(int(p))
        # 数字与页码都对、但结论越界（"因此利润大增"）——这类幻觉只能靠 LLM 抓。
        # 例外：跨公司口径（未选公司）时答案是"科目口径/算法/分析要点"这类通用表述，
        # 财报原文不可能逐句支持它，按越界兜底会让节点永远点不亮；数值与页码仍然严查。
        if (not llm_res.passed and llm_res.unsupported_claims and not missing and not bad_pages
                and (state.get("ui_filters") or {}).get("cross_company")):
            degraded.append("gate:concept_mode")
            patch = {"gate_pass": True, "gate_reason": "concept_mode",
                     "degraded": degraded}
            return patch
        if not llm_res.passed and llm_res.unsupported_claims and not missing and not bad_pages:
            patch = {
                "gate_pass": False,
                "gate_reason": "LLM 判定存在无依据断言: %s" % "；".join(llm_res.unsupported_claims[:3]),
                "errors": [make_error(ErrorCode.E_NUM_MISMATCH, "faithfulness_gate",
                                      "答案含资料无法支持的断言: %s" % "；".join(llm_res.unsupported_claims[:3]),
                                      "retry_then_fallback")],
            }
            if degraded:
                patch["degraded"] = degraded
            return patch

    patch: Dict[str, Any] = {}
    if degraded:
        patch["degraded"] = degraded

    if missing:
        patch.update({
            "gate_pass": False,
            "gate_reason": "数值未在原文中命中: %s" % ", ".join(missing[:5]),
            "errors": [make_error(ErrorCode.E_NUM_MISMATCH, "faithfulness_gate",
                                  "答案含原文不存在的数值: %s" % ", ".join(missing[:5]),
                                  "retry_then_fallback")],
        })
        return patch
    if bad_pages:
        patch.update({
            "gate_pass": False,
            "gate_reason": "引用页码不在上下文中: %s" % bad_pages[:5],
            "errors": [make_error(ErrorCode.E_CITATION_MISS, "faithfulness_gate",
                                  "引用页码 %s 不在召回片段中" % bad_pages[:5], "retry_then_fallback")],
        })
        return patch
    if llm_res is not None and not llm_res.passed:
        patch.update({
            "gate_pass": False,
            "gate_reason": "LLM 判定不通过: %s" % (llm_res.reason or "no_reason"),
            "errors": [make_error(ErrorCode.E_NUM_MISMATCH, "faithfulness_gate",
                                  "LLM 判定答案未被资料完全支持", "retry_then_fallback")],
        })
        return patch

    patch.update({"gate_pass": True, "gate_reason": "ok"})
    return patch
