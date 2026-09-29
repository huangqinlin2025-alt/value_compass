"""intent_router：金融垂类意图路由（规则底线 + LLM JSON 增强）。

为什么规则与 LLM 并存：
1. **合规红线由规则硬覆盖**：买卖/荐股/目标价这类词命中即 OOS，LLM 无权"放行"，可审计；
2. **规则足够确定时不打扰 LLM**：省 token、省延迟，Mock/无 Key 环境也能跑通；
3. **规则犹豫时才问 LLM**：低置信 / 多意图并列 / 语义绕弯的表述交给 IntentResult 契约判定；
4. LLM 失败或输出脏数据 -> 回落规则结果，只写 degraded，绝不阻断链路。

意图类别：
METRIC(指标查询) / TABLE(表格明细) / COMPARE(同比对比) / CALC(计算类) /
QUALITATIVE(定性分析) / SUMMARY(摘要) / CHITCHAT(闲聊) / OOS(越界) / UNCLEAR(不确定)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ...config import CONFIG
from ...decorators import safe_node
from ...prompts import build_router_messages
from ...providers import get_llm_provider
from ...schema import IntentResult
from ...text_utils import tokenize
from ...ui import (
    STAR_NODES,
    UI_ACTION_FOLLOWUP,
    UI_ACTION_STAR,
    clean_ui_filters,
    intent_for_node,
    known_node_ids,
    star_query,
    to_filters,
)

# (意图, 正则, 权重)
RULES: List[Tuple[str, str, float]] = [
    # 合规红线优先
    ("OOS", r"买入|卖出|买吗|卖吗|推荐|荐股|涨停|跌停|目标价|抄底|加仓|减仓|什么时候买|能涨|行情|预测.*股价|明天|下周", 1.0),
    ("CHITCHAT", r"^(你好|您好|hi|hello|谢谢|感谢|你是谁|你能做什么|帮助|help)\s*[。!！?？]*$", 0.9),
    ("SUMMARY", r"总结|概述|概括|摘要|要点|一句话|主要讲了|核心内容|帮我看看.*报告", 0.8),
    ("CALC", r"计算|算一?下|占比|比例|增长率|同比|环比|是多少.*%|率是多少", 0.7),
    ("COMPARE", r"对比|相比|比较|同比|环比|增长|下降|变化|趋势|较上年|较去年", 0.7),
    ("TABLE", r"明细|构成|列表|表格|有哪些|分别|各项|科目", 0.6),
    ("METRIC", r"营业收入|净利润|毛利率|净利率|资产|负债|现金流|费用|利润|收入|金额|净资产|收益率|周转|存款|存货|eps|每股收益|是多少|多少", 0.6),
    ("QUALITATIVE", r"为什么|原因|风险|战略|业务|说明|描述|如何看待|影响|优势|劣势", 0.5),
]

# 每种意图对应的检索配置（权重 / top_k / 是否偏好表格）
ROUTE_CFG: Dict[str, Dict[str, Any]] = {
    "METRIC": {"w_bm25": 1.3, "w_vector": 1.0, "w_meta": 0.8, "top_k": 8, "prefer_table": True},
    "TABLE": {"w_bm25": 1.2, "w_vector": 0.9, "w_meta": 0.9, "top_k": 8, "prefer_table": True},
    "COMPARE": {"w_bm25": 1.1, "w_vector": 1.1, "w_meta": 0.7, "top_k": 8, "prefer_table": True},
    "CALC": {"w_bm25": 1.2, "w_vector": 1.0, "w_meta": 0.7, "top_k": 8, "prefer_table": True},
    "QUALITATIVE": {"w_bm25": 0.9, "w_vector": 1.3, "w_meta": 0.6, "top_k": 6, "prefer_table": False},
    "SUMMARY": {"w_bm25": 0.8, "w_vector": 1.3, "w_meta": 0.9, "top_k": 10, "prefer_table": False},
    "GENERAL": {"w_bm25": 1.0, "w_vector": 1.0, "w_meta": 0.6, "top_k": 8, "prefer_table": False},
    # 星图点击：meta 权重最高（ui_filters 是绝对过滤），候选池收窄以压低延迟
    "STAR": {"w_bm25": 1.3, "w_vector": 1.0, "w_meta": 1.2, "top_k": 6, "prefer_table": True},
}

DEFAULT_CFG = ROUTE_CFG["GENERAL"]

# 合规红线：规则命中后 LLM 不得推翻
HARD_INTENTS = ("OOS", "CHITCHAT")


def classify(query: str) -> Dict[str, float]:
    q = query or ""
    scores: Dict[str, float] = {}
    for intent, pattern, weight in RULES:
        hits = len(re.findall(pattern, q, flags=re.I))
        if hits:
            scores[intent] = round(min(1.0, weight * (1.0 + 0.1 * (hits - 1))), 3)
    if not scores:
        scores["QUALITATIVE"] = 0.3
    return dict(sorted(scores.items(), key=lambda x: -x[1]))


def rule_judge(query: str) -> Dict[str, Any]:
    """规则打分：返回 (intent, confidence, margin, scores)。"""
    scores = classify(query)
    ranked = list(scores.items())
    top_intent, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    total = sum(scores.values()) or 1.0
    return {
        "intent": top_intent,
        "confidence": round(top_score / total, 3),
        "margin": round(top_score - second, 3),
        "scores": scores,
    }


def _star_patch(state: Dict[str, Any]) -> Dict[str, Any]:
    """星图点击 / 澄清按钮 = 确定性结构化事件：不打扰 LLM，直接产出意图与检索条件。

    与规则路由的关系：这里连规则打分都不做——节点 id 就是意图，省掉一次 LLM 往返
    （点击是高频交互，省下的延迟直接体现在 UI 上），同时杜绝"点毛利率被判成闲聊"的抖动。
    """
    ui_filters = clean_ui_filters(state.get("ui_filters") or {})
    node_id = str(ui_filters.get("node_id") or "").strip()
    degraded: List[str] = []

    # 白名单 = 字典 + 本会话经探针验证的新节点。会话节点不标 unknown——
    # 它已经验证过能召回，标了会让前端以为出了问题。
    session_nodes = state.get("session_nodes") or {}
    if node_id and node_id not in known_node_ids(session_nodes):
        # 未知节点不阻断：降级成"拿 node_id 当检索词"，并留标记供前端排查
        degraded.append("ui:unknown_star_node")

    intent = intent_for_node(node_id, session_nodes)
    q = star_query(node_id, ui_filters, session_nodes)
    cfg = dict(ROUTE_CFG.get("STAR", DEFAULT_CFG))
    cfg.update({"source": "ui_star", "rule_intent": intent, "intent_override": intent})

    patch: Dict[str, Any] = {
        "star_node_id": node_id,
        "intent": intent,
        "intent_confidence": 1.0,
        "intent_scores": {intent: 1.0},
        "route_cfg": cfg,
        # ui_filters 作为绝对过滤条件：filters_strict=True 时召回为空也不回退
        "filters": to_filters(ui_filters),
        "filters_strict": True,
        "query_rewritten": q,
        "query_terms": tokenize(q)[:200],
    }
    if degraded:
        patch["degraded"] = degraded
    return patch


def _llm_intent(query: str, history: List[Dict[str, str]], rule: Dict[str, Any]) -> Optional[IntentResult]:
    """只有配置开启且 provider 支持 JSON 时才调用；任何异常都交给调用方回落。"""
    if not CONFIG.router_llm_enabled:
        return None
    # provider 构造失败（无 Key / 依赖缺失）直接抛出，由节点捕获后回落规则并写 degraded
    llm = get_llm_provider(allow_fallback=False)
    if not getattr(llm, "supports_json", False):
        return None  # 如 MockLLM：不支持 JSON 就不打扰，不算降级
    messages = build_router_messages(
        query,
        history=history,
        rule_hint={"intent": rule["intent"], "confidence": rule["confidence"], "scores": rule["scores"]},
    )
    return llm.generate_json(messages, IntentResult, key="router", timeout=CONFIG.timeout_llm)


@safe_node("intent_router", timeout=5.0, fallback_patch={
    "intent": "UNCLEAR", "intent_confidence": 0.0, "route_cfg": DEFAULT_CFG,
})
def intent_router(state: Dict[str, Any]) -> Dict[str, Any]:
    # ---- 短路：星图点击 / 澄清按钮补充条件，均为结构化事件，跳过 LLM JSON 解析 ----
    if state.get("ui_action") in (UI_ACTION_STAR, UI_ACTION_FOLLOWUP):
        return _star_patch(state)

    query = state.get("query_rewritten") or state.get("query_raw") or ""
    history = state.get("history") or []
    rule = rule_judge(query)

    final_intent = rule["intent"]
    confidence = rule["confidence"]
    source = "rule"
    degraded: List[str] = []

    # 规则足够确定 or 命中合规红线 -> 不打扰 LLM
    need_llm = (
        rule["intent"] not in HARD_INTENTS
        and (rule["confidence"] < CONFIG.intent_confidence_min or rule["margin"] < CONFIG.intent_margin_min)
    )
    if need_llm:
        try:
            res = _llm_intent(query, history, rule)
        except Exception:
            res = None
            degraded.append("router:llm_fallback_rule")
        if res is not None:
            # 取严：LLM 判越界一律采纳；否则只在 LLM 有把握时才覆盖规则
            if res.intent == "OOS" or rule["intent"] == "OOS":
                final_intent = "OOS"
                confidence = max(confidence, res.confidence)
            elif res.intent != "UNCLEAR" and res.confidence >= 0.5:
                final_intent = res.intent
                confidence = res.confidence
            elif res.intent == "UNCLEAR":
                final_intent = "UNCLEAR"
                confidence = min(confidence, res.confidence)
            source = "llm"
            if res.filters:
                merged_filters = dict(state.get("filters") or {})
                merged_filters.update({k: v for k, v in res.filters.items() if v})
                filters_patch = merged_filters
            else:
                filters_patch = None
        else:
            filters_patch = None
    else:
        filters_patch = None

    cfg = dict(ROUTE_CFG.get(final_intent, DEFAULT_CFG))
    if rule["margin"] < CONFIG.intent_margin_min and final_intent not in HARD_INTENTS:
        cfg = dict(DEFAULT_CFG)
    cfg["source"] = source
    cfg["rule_intent"] = rule["intent"]

    patch: Dict[str, Any] = {
        "intent": final_intent,
        "intent_confidence": confidence,
        "intent_scores": rule["scores"],
        "route_cfg": cfg,
    }
    if filters_patch:
        patch["filters"] = filters_patch
    if degraded:
        patch["degraded"] = degraded
    return patch
