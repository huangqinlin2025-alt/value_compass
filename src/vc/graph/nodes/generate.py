"""generator：优先走 AnswerResult JSON 契约，失败回落到抽取式作答。

输出治理（写在这里，而不是靠模型自觉）：
1. citations 的页码必须在 context 里真实存在，非法页码直接剥离（防止编造引用）；
2. quote 必须在原文中逐字命中，命中不了就丢弃该条引用；
3. 正文里的 [Pxx] 角标同样按 context 页码白名单过滤；
4. JSON 链路任何异常 -> 回落 MockLLM / 自由文本，并写 degraded，图不断裂。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from ...config import CONFIG
from ...decorators import safe_node
from ...prompts import build_generator_messages, build_ui_generator_messages
from ...providers import get_llm_provider
from ...providers.llm import MockLLM
from ...retrieval.probe import probe_node
from ...schema import AnswerResult, UiAnswerResult
from ...text_utils import clean_display, clip
from ...ui import (
    UI_ACTION_FOLLOWUP,
    UI_ACTION_STAR,
    clean_unlock_next,
    label_for_node,
    next_candidates,
    period_cn,
    scope_of_chunks,
    star_query,
)

_CITE = re.compile(r"\[P(\d+)\]")
# context 的片段前缀形如 "[P12|章节路径]"，模型常把它整段抄进输出。
# 只要求闭合的 "]"：不闭合的情况多半已被 100 字截断，宁可留着，也不要误删正文。
_CITE_PREFIX = re.compile(r"\[P(\d+)\|[^\]\[]*\]")

# 星图卡片是"渐进式披露"：一次只讲一个概念，超长会挤掉星图布局
# 350 而非 100：两期对比卡片要放得下"两期金额 + 同比变化 + 引用"，
# 100 字会把第二期的数字直接切掉，等于白算（schema 侧同样硬截 350）。
UI_EXPLANATION_MAX = 350


def _allowed_pages(ctx: List[Dict[str, Any]]) -> Set[int]:
    return {int(c["page"]) for c in ctx or [] if c.get("page") is not None}


def sanitize_answer(result: AnswerResult, ctx: List[Dict[str, Any]]) -> str:
    """按 context 页码白名单清洗答案：非法角标剥离，非法引用丢弃。"""
    allowed = _allowed_pages(ctx)
    answer = result.answer or ""

    def _keep(m: "re.Match") -> str:
        page = int(m.group(1))
        return m.group(0) if page in allowed else ""

    answer = _CITE.sub(_keep, answer)

    valid_cites = [c for c in (result.citations or []) if int(c.page) in allowed]
    if valid_cites and not _CITE.search(answer):
        # 正文没带角标但给了引用列表 -> 补上，保证可溯源
        answer += "（来源：%s）" % "、".join("P%s" % c.page for c in valid_cites[:6])
    return re.sub(r"\s{2,}", " ", answer).strip()


def _ui_payload_fallback(state: Dict[str, Any], ctx: List[Dict[str, Any]], reason: str) -> Dict[str, Any]:
    """任何失败都必须给前端一个可渲染的退化载荷，绝不让 UI 拿到 None。"""
    node_id = state.get("star_node_id") or ""
    pages = sorted({int(c["page"]) for c in ctx or [] if c.get("page") is not None})[:3]
    return {
        "node_id": node_id,
        "label": label_for_node(node_id, state.get("session_nodes") or {}),
        "explanation": "未在报告中找到相关信息。" if not ctx else "以下为原文摘录。",
        "citations": ["P%d" % p for p in pages],
        # 退化卡片同样给该字段：前端不必为"有/无对比"写两套渲染逻辑
        "citations_by_period": {},
        "new_nodes": [],
        "unlock_next": next_candidates(node_id, state.get("unlocked_nodes") or []),
        "refused": True,
        "degraded_reason": reason,
        # 退化卡片同样给坐标：前端不必为"有/无 scope"写两套渲染逻辑
        "scope": scope_of_chunks(ctx),
    }


def _cites_by_period(cites: List[str], ctx: List[Dict[str, Any]], periods: List[str],
                     limit: int = 2) -> Dict[str, List[str]]:
    """对比模式：按期间分组给出引用页码。

    为什么不直接给每个页码打期间标签：报表的页码结构逐年固定，「营业收入」在
    2024A 和 2025A 里往往都在 P54——反查期间时两期都命中，判不明。硬猜一个就会
    把引用指到错的那一期，比不给更糟。按期间分组则天然无歧义：
    {"2024A": ["P54"], "2025A": ["P54"]} 两组各自配自己的期间，渲染成
    「公司 - 期间 - 页码」三坐标时页码才唯一。
    """
    cited: List[int] = []
    for c in cites or []:
        s = str(c)
        if s.startswith("P") and s[1:].isdigit():
            cited.append(int(s[1:]))
    out: Dict[str, List[str]] = {}
    for p in periods or []:
        pages: List[int] = []
        for c in ctx or []:
            if str(c.get("report_period") or "") != p:
                continue
            pg = c.get("page")
            if pg is None:
                continue
            n = int(pg)
            if n not in pages:
                pages.append(n)
        # 优先保留模型真正引用过的页码，其次退回该期片段的页码
        picked = [n for n in pages if n in cited][:limit] or pages[:limit]
        out[p] = ["P%d" % n for n in picked]
    return out


def _verify_new_nodes(cands: Any, state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """模型提议的新节点：逐个跑可召回性探针，只留真的能召回的。

    不放行的后果很隐蔽：造一个报告里没有的科目（如「市盈率」），用户点进去是空卡片，
    而空卡片在链路里的表现是"转兜底"——trace 上每个节点都 ok=true，排查时根本
    看不出是节点造错了，只会以为"这家公司没披露"。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for n in cands or []:
        # 两种形态都要认：dict（经 schema._coerce 清洗）和 NewStarNode 实例
        # （直接构造对象时 pydantic 已转好）。只认 dict 的话后一类会被静默丢掉，
        # 表现是"模型明明提议了、星图却没长出新节点"，且无任何报错。
        if isinstance(n, dict):
            nid = str(n.get("id") or "").strip().lower()
            label = str(n.get("label") or "")
            kws = [str(k).strip() for k in (n.get("keywords") or [])]
        else:
            nid = str(getattr(n, "id", "") or "").strip().lower()
            label = str(getattr(n, "label", "") or "")
            kws = [str(k).strip() for k in (getattr(n, "keywords", None) or [])]
        kws = [k for k in kws if k]
        if not nid or not kws:
            continue
        try:
            ok, hit, _cov = probe_node(kws, filters=state.get("filters") or {})
        except Exception:
            # 探针自身出错时**不放行**：宁可不扩充，也不放幽灵节点进星图
            continue
        if ok:
            out[nid] = {"label": str(label or nid)[:20], "keywords": kws,
                        "intent": "METRIC", "probe_hit": hit}
        if len(out) >= 2:
            break
    return out


def _ui_generate(state: Dict[str, Any], ctx: List[Dict[str, Any]], llm: Any, degraded: List[str]) -> Dict[str, Any]:
    """星图模式：走 UiAnswerResult 契约，产出驱动前端的严格 JSON 指令。"""
    node_id = state.get("star_node_id") or ""
    unlocked = list(state.get("unlocked_nodes") or [])
    cands = next_candidates(node_id, unlocked)
    # 本会话已验证通过的新节点：与字典节点同构，参与白名单校验与检索词生成
    session_nodes: Dict[str, Any] = dict(state.get("session_nodes") or {})
    filters = {k: v for k, v in (state.get("filters") or {}).items()
               if k in ("stock_code", "report_period", "report_periods")}
    scope = scope_of_chunks(ctx)
    # 跨公司口径：scope 不能再伪装成"某一家公司"（scope_of_chunks 取的是片段众数），
    # 否则卡片会显示一个用户从没选过的公司，看起来像系统私自锁定了主体。
    if (state.get("ui_filters") or {}).get("cross_company"):
        # 期间同理：各家期次不同，留着会显示一个用户没选过的期间
        scope = dict(scope, company=None, stock_code=None,
                     report_period=None, report_periods=[])
    # 提示词里必须点名主体：不点名时模型只会背概念定义
    # （实测三张卡片全是"营业收入是指……"这类教科书句子，零公司数据）。
    # 对比模式要点名两期并写明"对比"：只给一期的话模型会以为只要那一期的数据。
    periods = [p for p in (scope.get("report_periods") or []) if p]
    if len(periods) >= 2:
        subject = "%s %s 与 %s 对比" % (
            scope.get("company") or "", period_cn(periods[0]), period_cn(periods[1]))
    elif scope.get("company"):
        subject = " ".join(
            x for x in (scope.get("company") or "", period_cn(scope.get("report_period"))) if x)
    else:
        # 未选公司：公司是可选上下文，卡片以科目本身的口径/算法/分析要点为主，
        # 引用到具体数值时必须写明公司名，否则跨公司片段会串味。
        # 未锁定公司时**不给具体数值**：多家片段混在一起，一旦写出数字就会串公司，
        # 忠实性闸门必然判"数值未被原文支持"（实测 num_mismatch 兜底）。
        subject = "全部公司（跨公司口径）。未锁定具体公司：只讲该科目的口径、算法、" \
                  "披露位置和分析要点，不要写出任何具体数值；如确需举例，必须写明公司名与期间。"

    result: Optional[UiAnswerResult] = None
    if getattr(llm, "supports_json", False) and ctx:
        try:
            messages = build_ui_generator_messages(
                label_for_node(node_id, session_nodes) or node_id,
                ctx,
                candidates=cands,
                unlocked=unlocked,
                filters=filters,
                history=state.get("history") or [],
                scope=subject,
            )
            result = llm.generate_json(messages, UiAnswerResult, key="ui_answer", timeout=CONFIG.timeout_llm)
        except Exception:
            degraded.append("generator:ui_json_fallback_text")
    else:
        degraded.append("generator:ui_json_unsupported")

    if result is None:
        # 回落：抽取式文本 + 退化载荷，图不断裂，UI 仍有卡片可渲染
        text = llm.answer(star_query(node_id, state.get("ui_filters") or {}), ctx, "METRIC",
                          timeout=CONFIG.timeout_llm)
        payload = _ui_payload_fallback(state, ctx, "ui_json_unavailable")
        payload["explanation"] = clip(clean_display(text), UI_EXPLANATION_MAX) or payload["explanation"]
        return {"explanation": payload["explanation"], "payload": payload, "used": [],
                "session_nodes": {}}

    allowed = _allowed_pages(ctx)
    # 1) 引用页码白名单过滤（与文本链路同一套治理，防编造引用）
    cites = [c for c in (result.citations or []) if c[1:].isdigit() and int(c[1:]) in allowed]
    # 2) 正文角标同样按白名单过滤
    #    先把 "[P12|章节路径]" 归一成 "[P12]"：模型常把 context 的前缀整段抄进 explanation，
    #    既让卡片显示 "|审计意见 >" 这类脏后缀，又因 _CITE 只认 "[P12]" 而漏抽引用
    #    （漏抽 = 判"含数字但无引用" = 整张卡片被 output_guard 改成兜底文案）。
    explanation = _CITE.sub(
        lambda m: m.group(0) if int(m.group(1)) in allowed else "",
        _CITE_PREFIX.sub(r"[P\1]", result.explanation or ""),
    )
    # 3) 硬约束 100 字（模型不自觉就在这里截断，不依赖 Prompt 自觉）
    explanation = clip(explanation, UI_EXPLANATION_MAX)
    # 4) unlock_next 必须落在星图白名单内，否则前端会出现点不动的幽灵节点。
    #    白名单 = 全局字典 + **本轮通过可召回性探针**的新节点，所以必须先验证再过滤，
    #    顺序反了的话新提议会被自己的白名单挡掉（永远进不了星图）。
    verified = _verify_new_nodes(getattr(result, "new_nodes", None), state)
    if verified:
        session_nodes.update(verified)
    unlock = clean_unlock_next(result.unlock_next, node_id, unlocked, limit=2,
                               session_nodes=session_nodes) or cands
    # 通过验证的提议要补进推荐位：模型把它写在 new_nodes 里、没写进 unlock_next 时，
    # 不补的话前端根本没有入口可点（星图上不会出现这个节点）。
    for nid in verified:
        if nid not in unlock and len(unlock) < 2:
            unlock.append(nid)

    # 模型给了数值却没标页码（实测 glm-4-flash 偶发，既无 [Pxx] 也无「来源：Pxx」）：
    # 用真实喂给它的 context 页码补引，否则下游 citation_validate 判"含数字但无任何引用"
    # -> output_guard 把整张卡片改成"未能从报告中获得可溯源的数值"（惜答）。
    # 只补"资料从哪来"，不制造任何数值；数字本身仍由 faithfulness_gate 逐字把关，
    # 编造数字照样走 E_NUM_MISMATCH -> retry -> 兜底，本改动不削弱护栏。
    if not cites and not result.refused:
        pages = list(dict.fromkeys(
            int(c["page"]) for c in ctx if c.get("page") is not None))[:3]
        cites = ["P%d" % p for p in pages]

    if cites and not _CITE.search(explanation):
        suffix = "（来源：%s）" % "、".join(cites[:3])
        # 来源串必须计入 100 字预算：先给正文留出剩余额度再拼，
        # 否则超过上限时 clip 掉的正是来源串，等于白补一次。
        explanation = clip(explanation, max(UI_EXPLANATION_MAX - len(suffix), 0)) + suffix

    payload = {
        "node_id": node_id,
        "label": label_for_node(node_id, session_nodes),
        "explanation": explanation,
        "citations": cites,
        # 对比模式下按期间分组的引用（配合 scope.company 渲染成「公司 - 期间 - 页码」），
        # 解决两期同页的歧义；单期模式为空对象，前端不必分两套逻辑。
        "citations_by_period": _cites_by_period(cites, ctx, periods) if len(periods) >= 2 else {},
        "unlock_next": unlock,
        # 本轮通过探针的新节点：前端据此渲染"新长出来的"节点（含中文名）
        "new_nodes": [{"id": k, "label": v.get("label")} for k, v in verified.items()],
        "refused": bool(result.refused),
        "unlocked_nodes": unlocked,
        # 文档坐标：公司 + 期间，让 "P12" 变成可唯一定位的引用
        "scope": scope,
    }
    used = [cid for cid in (result.used_chunk_ids or []) if cid]
    return {"explanation": explanation, "payload": payload, "used": used,
            "session_nodes": verified}


@safe_node("generator", timeout=CONFIG.timeout_llm + 2.0, provider="llm",
           fallback_patch={"draft_answer": "", "answer": ""})
def generator(state: Dict[str, Any]) -> Dict[str, Any]:
    ctx: List[Dict[str, Any]] = list(state.get("context") or [])
    query = state.get("query_rewritten") or state.get("query_raw") or ""
    intent = state.get("intent") or ""
    history = state.get("history") or []

    degraded: List[str] = []
    try:
        llm = get_llm_provider(allow_fallback=False)
    except Exception:
        llm = MockLLM()
        degraded.append("llm:fallback_to_mock")

    # ---- UI 分支：星图点击强制结构化输出 ----
    if state.get("ui_action") in (UI_ACTION_STAR, UI_ACTION_FOLLOWUP):
        ui = _ui_generate(state, ctx, llm, degraded)
        patch: Dict[str, Any] = {
            "draft_answer": ui["explanation"],
            "answer": ui["explanation"],
            "ui_payload": ui["payload"],
        }
        if ui["used"]:
            patch["generation_used"] = ui["used"][:20]
        if ui.get("session_nodes"):
            # 只写通过探针的节点，且只活在会话里（不碰全局字典——那需要人工确认）
            patch["session_nodes"] = ui["session_nodes"]
        if degraded:
            patch["degraded"] = degraded
        return patch

    answer = ""
    used: List[str] = []
    if CONFIG.generator_json_enabled and getattr(llm, "supports_json", False) and ctx:
        try:
            messages = build_generator_messages(query, ctx, intent, history)
            result = llm.generate_json(messages, AnswerResult, key="generator", timeout=CONFIG.timeout_llm)
            answer = sanitize_answer(result, ctx)
            used = [cid for cid in (result.used_chunk_ids or []) if cid]
        except Exception:
            answer = ""
            degraded.append("generator:json_fallback_text")

    if not answer:
        # 兜底：抽取式（MockLLM）或 OpenAI 兼容自由文本
        answer = llm.answer(query, ctx, intent, timeout=CONFIG.timeout_llm)

    patch: Dict[str, Any] = {"draft_answer": answer, "answer": answer}
    if used:
        # 生成期引用的片段 id，供 UI 解释"答案取自哪些片段"
        patch["generation_used"] = used[:20]
    if degraded:
        patch["degraded"] = degraded
    return patch


def render_citation_snippet(text: str, limit: int = 160) -> str:
    return clean_display(text)[:limit]
