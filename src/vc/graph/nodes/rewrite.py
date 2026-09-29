"""query_rewrite：指代消解 + 金融同义扩展 + 元数据过滤抽取。

为什么必须先改写：
1. 多轮追问"那同比呢？"缺主语，直接召回必然漂移；
2. 财报术语口语化严重（"营收"→"营业收入"、"净利"→"净利润"），不扩展会漏召回；
3. 过滤条件（公司/期间/章节）在这里一次抽好，三路召回共用，保证结果一致。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from ...config import CONFIG
from ...decorators import safe_node
from ...errors import ErrorCode, make_error
from ...knowledge import company_options, default_periods, known_meta, latest_period, name_of
from ...retrieval.filters import extract_filters
from ...state import RESET_LIST
from ...text_utils import tokenize
from ...ui import (
    OOS_STOCK_CODE,
    UI_ACTION_CREATE,
    UI_ACTION_FOLLOWUP,
    UI_ACTION_LINK,
    UI_ACTION_STAR,
    clean_ui_filters,
    match_nodes_by_query,
    star_query,
    to_filters,
)

SYNONYMS: Dict[str, List[str]] = {
    "营收": ["营业收入", "主营业务收入"],
    "净利": ["净利润", "归属于上市公司股东的净利润"],
    "毛利": ["毛利率", "营业毛利"],
    "负债率": ["资产负债率"],
    "roe": ["净资产收益率", "ROE"],
    "roa": ["总资产收益率", "ROA"],
    "现金流": ["现金流量", "经营活动产生的现金流量净额"],
    "eps": ["每股收益", "基本每股收益"],
    "存货": ["存货", "库存"],
    "应收": ["应收账款", "应收票据"],
}

_PRONOUNS = ("它", "其", "这家公司", "该公司", "这", "那", "同比", "环比", "同上", "上述")

# 自然语言回合必须清掉的 UI 残留（这几个都是覆盖写字段，Checkpointer 会原样恢复上一轮的值）：
# - ui_payload：不清会渲染出"上轮节点 + 本轮引用"的串味卡片；
# - star_node_id：不清会让自然语言提问去点亮上一轮点过的节点（星图被误点亮）；
# - filters_strict：不清会让自然语言提问也继承"过滤后为空不回退"的严格策略。
# 每轮第一个节点统一清一次，比在每个下游节点里各判一次更可靠。
_NATURAL_TURN_RESET: Dict[str, Any] = {
    "ui_payload": {},
    "star_node_id": "",
    "filters_strict": False,
}


def _expand(query: str) -> str:
    out = query
    for k, vs in SYNONYMS.items():
        if k in out.lower():
            out += " " + " ".join(vs)
    return out


def _coref(query: str, history: List[Dict[str, str]]) -> str:
    if not history:
        return query
    if not any(p in query for p in _PRONOUNS):
        return query
    last_q = ""
    for h in reversed(history):
        if h.get("role") == "user":
            last_q = h.get("content", "")
            break
    if not last_q:
        return query
    # 从上一轮抽取关键实体（4 字以上中文词 / 指标词），拼到本轮前面
    entities = re.findall(r"[\u4e00-\u9fff]{2,8}(?:收入|利润|资产|负债|现金流|费用|率|额)", last_q)
    entities += re.findall(r"20\d{2}\s*年?(?:半年度|年度|一季度|三季度)?", last_q)
    prefix = " ".join(dict.fromkeys(entities))
    return (prefix + " " + query).strip() if prefix else query


def _resolve_company(ui_filters: Dict[str, Any]) -> Dict[str, Any]:
    """把 company（中文名或代码）解析成可硬过滤的 stock_code。

    `clean_ui_filters` 只认 4-6 位数字：「贵州茅台」这类中文名取不到代码，
    于是三路召回没有硬过滤条件——向量路的 `where` 为 None，必然按语义召回别家公司的片段，
    点「某公司营业收入」会拿到通用定义或第三方报表原文（跨公司串味）。

    - 库内标的：补上 stock_code，让硬过滤真正生效（三路都能锁定目标公司的文档）；
    - 库外标的：注入 OOS_STOCK_CODE，配合 filters_strict 把三路打空 → 转兜底，
      而不是拿别人家的资料冒充成该公司的答案。
    """
    if ui_filters.get("stock_code") or not ui_filters.get("company"):
        return ui_filters
    out = dict(ui_filters)
    resolved = extract_filters(str(out["company"]), known=known_meta())
    out["stock_code"] = str(resolved.get("stock_code") or "") or OOS_STOCK_CODE
    return out


def _resolve_scope(
    ui_filters: Dict[str, Any], state: Dict[str, Any]
) -> Tuple[Dict[str, Any], bool]:
    """星图点击的作用域：公司必填、期间默认最新一期。

    为什么必须在这里补：语料是「多家公司 × 多期」，前端只传 node_id 时三路召回
    没有任何硬过滤，向量路 `where=None` 会按语义召回各家公司同一科目的片段
    （实测点「风险因素」一次混进隆基/药明/平安/伊利 4 家），卡片只能归纳出
    "XX 是指……"的通用定义，而页码在多份文档间还是歧义 ID（P2 属于好几家公司）。

    公司取值优先级：显式 company > 会话已锁定的公司 > 库内唯一一家 > 跨公司口径。
    最后一种**不再拦住点亮**：公司是可选上下文，不是前置门槛——用户点节点要看的是
    这个科目本身（口径/算法/分析要点），公司数据只是实例补充。此时走跨公司召回，
    由生成侧要求"引用数值必须写明公司名"，串味风险用标注化解而不是用拒答回避。

    期间只做**软偏好**（写进 prefer_period，不进 filters）：实测硬锁定期会把召回
    打空——某期可能根本没披露该指标（茅台 2025A 的"毛利率"只召回 6 条，
    IDF 相关性不过线 -> 转兜底）。硬过滤公司已经消除了最严重的跨公司串味，
    期间交给检索词偏向 + 卡片 scope 标注主导期即可。

    返回 (ui_filters, need_company)。
    """
    out = dict(ui_filters)
    if not out.get("stock_code") and not out.get("company"):
        sess = str(state.get("session_company") or "").strip()
        if sess:
            out["company"] = name_of(sess) or sess
            out["stock_code"] = sess
        else:
            opts = company_options()
            if len(opts) == 1:
                only = opts[0]
                out["company"] = only.get("short_name") or only.get("company") or only["stock_code"]
                out["stock_code"] = only["stock_code"]
            else:
                out["cross_company"] = True
                out.pop("company", None); out.pop("stock_code", None)

    out = _resolve_company(out)
    explicit = [str(p) for p in (out.get("report_periods") or []) if str(p)]
    if len(explicit) >= 2:
        # 对比模式：两期都走硬过滤，检索词**不带期间词**——带任一期的中文年份都会
        # 把召回偏向那一期（另一期只剩零星片段，卡片就缺一半数据），同时还会拉低
        # IDF 覆盖率。期间定位完全交给 report_periods 的硬过滤。
        out["report_periods"] = explicit[:2]
        out.pop("prefer_period", None)
    elif len(explicit) == 1:
        out["report_period"] = explicit[0]
        out.pop("report_periods", None)
    elif out.get("compare"):
        # 要对比但没指定哪两期：给"最新 + 去年同期"（同口径，见 default_periods）
        ps = default_periods(str(out.get("stock_code") or ""), 2)
        if len(ps) >= 2:
            out["report_periods"] = ps[:2]
            out.pop("prefer_period", None)
        elif ps:
            out["prefer_period"] = ps[0]
    elif not out.get("report_period"):
        # 库外公司（哨兵）查不到任何期次，latest_period 返回空 -> 不加期间，交给兜底
        period = latest_period(str(out.get("stock_code") or ""))
        if period:
            out["prefer_period"] = period
    return out, False


# 连边意图的触发词。刻意**不含**"有什么关系"——那是提问（用户想知道两者如何关联），
# 不是建边命令（用户要真的在图上画一条线）。把提问误判成建边，会在用户只想要一个
# 解释的时候悄悄改动他的星图，且改完他还得自己发现。
_LINK_TRIGGERS = ("连起来", "连接", "连到", "连一条", "建立关联", "关联起来", "关联到")


def _try_link_intent(raw: str, session_nodes: Dict[str, Any] = None) -> Tuple[str, str]:
    """自然语言 -> 连边意图。返回 (源节点, 目标节点)；不是连边则返回 ("", "")。

    判定要同时满足两个条件：句中有连边触发词 **且** 能解析出两个不同节点。
    只看触发词会在"关联交易怎么连起来的"这类句子上误判。
    """
    q = str(raw or "")
    if not any(t in q for t in _LINK_TRIGGERS):
        return "", ""
    both = match_nodes_by_query(q, session_nodes, limit=2)
    if len(both) < 2 or both[0] == both[1]:
        return "", ""
    return both[0], both[1]


@safe_node("query_rewrite", timeout=1.0, fallback_patch={
    "query_rewritten": "",
    "query_terms": [],
    "filters": {},
})
def query_rewrite(state: Dict[str, Any]) -> Dict[str, Any]:
    # 所有分支共用：前端载荷必须先过白名单，脏 key 不进主链路
    ui_filters = clean_ui_filters(state.get("ui_filters") or {})

    # ---- 新建节点：只做探针验证，不召回、不生成 ----
    if state.get("ui_action") == UI_ACTION_CREATE:
        return {
            "star_node_id": str(ui_filters.get("node_id") or ""),
            "ui_filters": ui_filters,
            "query_rewritten": "",
            "query_terms": [],
            "filters": {},
            "errors": RESET_LIST,
            "trace": RESET_LIST,
        }

    # ---- 连边：纯拓扑操作，不需要召回，因此不做作用域解析 ----
    # 若走下面的结构化分支，会去解析公司/期间；库里有多个公司时还会注入 OOS 哨兵，
    # 最终把三路打空——而连边根本不检索，这些步骤全是噪声。
    if state.get("ui_action") == UI_ACTION_LINK:
        return {
            "star_node_id": str(ui_filters.get("node_id") or ""),
            "ui_filters": ui_filters,
            "query_rewritten": "",
            "query_terms": [],
            "filters": {},
            "errors": RESET_LIST,
            "trace": RESET_LIST,
        }

    # ---- 结构化事件：绕过 query_raw，直接用 UI 载荷驱动召回 ----
    # 场景：用户在 clarify 卡片上点按钮补充条件后的第二次 invoke（query_raw 通常是空的），
    # 以及前端直接点亮星图节点。此时 query_raw 不是必需的。
    if state.get("ui_action") in (UI_ACTION_STAR, UI_ACTION_FOLLOWUP) and ui_filters:
        node_id = str(ui_filters.get("node_id") or "")
        ui_filters, need_company = _resolve_scope(ui_filters, state)
        degraded: List[str] = []
        if need_company:
            # 多家公司且会话未锁定 -> 注入哨兵把三路打空，走兜底提示先选公司。
            # 不能退回"不过滤"：那正是跨公司串味的来源。
            ui_filters["stock_code"] = OOS_STOCK_CODE
            ui_filters.pop("company", None)
            degraded.append("ui:need_company")
        q = star_query(node_id, ui_filters)
        patch: Dict[str, Any] = {
            "star_node_id": node_id,
            # 写回解析后的 ui_filters：下游 intent_router 的 _star_patch 从 state 读同一份，
            # 两边必须一致，否则 router 会用未解析的版本覆盖掉这里的 stock_code。
            "ui_filters": ui_filters,
            "query_rewritten": q,
            "query_terms": tokenize(q)[:200],
            "filters": to_filters(ui_filters),
            # 新回合：清掉 Checkpointer 里上一轮残留的 errors/trace
            "errors": RESET_LIST,
            "trace": RESET_LIST,
        }
        # 本轮真正确定的公司写入会话作用域，供后续点击继承（库外哨兵不写）
        code = str(ui_filters.get("stock_code") or "")
        if code and code != OOS_STOCK_CODE:
            patch["session_company"] = code
        if degraded:
            patch["degraded"] = degraded
        return patch

    raw = (state.get("query_raw") or "").strip()
    if not raw:
        return {
            # errors/trace 是累加型字段：本轮虽然只有一个错误，也必须带哨兵清掉上轮残留，
            # 否则 reset_starmap（query_raw 为空）会把上一轮检索的 trace 一直带着。
            "errors": RESET_LIST + [make_error(ErrorCode.E_INTERNAL, "query_rewrite", "空问题")],
            "trace": RESET_LIST,
            "query_rewritten": "",
            "query_terms": [],
            **_NATURAL_TURN_RESET,
        }
    # ---- 口述连边："把毛利率和净利润连起来" ----
    # 连边是纯拓扑操作，不检索，因此不改写、不抽取过滤条件。
    # 改写后的 ui_action 会被 route_after_rewrite 读到，从而直接进 link_commit。
    src, tgt = _try_link_intent(raw, state.get("session_nodes") or {})
    if src and tgt:
        return {
            "ui_action": UI_ACTION_LINK,
            "star_node_id": src,
            "ui_filters": dict(ui_filters, node_id=src, link_target=tgt),
            "query_rewritten": "",
            "query_terms": [],
            "filters": {},
            "errors": RESET_LIST,
            "trace": RESET_LIST,
        }

    history = state.get("history") or []
    base = _coref(raw, history)          # 指代消解后的"人说的话"
    rewritten = _expand(base)            # 同义扩展，只服务于召回
    # 相关性判定用扩展前的词，否则扩展词会稀释 IDF 相关性，导致误判不相关
    terms = tokenize(base)[:200]
    filters = extract_filters(rewritten, known=known_meta())
    patch: Dict[str, Any] = {
        "query_rewritten": rewritten,
        "query_terms": terms,
        "filters": filters,
        # 清掉上一轮星图点击留下的 UI 残留（语义见 _NATURAL_TURN_RESET）
        **_NATURAL_TURN_RESET,
        # 自然语言回合同样重置横切通道，避免 Checkpointer 无限累积（语义见 state.add_list）
        "errors": RESET_LIST,
        "trace": RESET_LIST,
    }
    # 自然语言里点名了公司（"茅台净利润"）就把会话锁定到它：
    # 用户接着点星图节点时，卡片应仍然是这家公司的数据。
    code = str(filters.get("stock_code") or "")
    if code:
        patch["session_company"] = code
    return patch
