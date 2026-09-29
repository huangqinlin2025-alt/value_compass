"""UI 事件契约层：前端「知识星图」与图之间的唯一翻译层。

边界（改 UI 不改图，改图不改 UI）：
1. 前端传来的 ui_action / ui_filters 一律先过这里清洗，脏数据不进主链路；
2. 星图节点 -> 检索关键词 / 意图 / 过滤条件 的映射只在这里维护；
3. 纯函数、无 IO、无 LLM，便于单测覆盖。

安全约定：白名单放行 + 类型收敛，前端传来的任意 key 都不会直接进检索过滤。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

# ---------- 前端事件类型 ----------
UI_ACTION_STAR = "click_star"                # 点亮星图节点
UI_ACTION_QUERY = "natural_query"            # 自然语言提问
UI_ACTION_FOLLOWUP = "clarify_followup"      # 在澄清卡片上点结构化按钮补充条件
UI_ACTION_RESET = "reset_starmap"            # 仅重置点亮进度（默认档）
UI_ACTION_RESET_ALL = "reset_starmap_all"    # 全部清除：进度 + 会话节点 + 会话边
UI_ACTION_LINK = "link_confirm"              # 用户确认建立会话边（"连接 A 与 B"）
UI_ACTION_CREATE = "create_node"             # 用户主动新建节点（可留空 → 草稿节点，内容后补）

# 会话节点的内容状态：草稿（占位，内容待补）。未带该字段即"已就绪，可点亮"。
DRAFT_STATUS = "draft"

UI_ACTIONS = (UI_ACTION_STAR, UI_ACTION_QUERY, UI_ACTION_FOLLOWUP, UI_ACTION_RESET,
              UI_ACTION_RESET_ALL, UI_ACTION_LINK, UI_ACTION_CREATE)

# ---------- 星图节点表 ----------
# label 给 UI 展示；keywords 给三路召回；intent 决定检索配方；next 是渐进式披露的推荐路径
# 字典是"打底"而非"封闭"：模型可提出新节点，但必须通过可召回性验证（见 probe_node）
# 才进入会话星图。入字典的标准是**任意 A 股定期报告必有的科目**——只部分公司披露的
# （如客户集中度、股权激励）不放，否则点了就是空卡片。
STAR_NODES: Dict[str, Dict[str, Any]] = {
    # ---------- 利润表 / 盈利能力 ----------
    "revenue": {
        "label": "营业收入", "intent": "METRIC",
        "keywords": ["营业收入", "主营业务收入"], "prefer_table": True,
        "next": ["net_profit", "gross_margin", "business_mix"],
    },
    "net_profit": {
        "label": "净利润", "intent": "METRIC",
        "keywords": ["净利润", "归属于上市公司股东的净利润"], "prefer_table": True,
        "next": ["roe", "cashflow", "eps"],
    },
    "gross_margin": {
        "label": "毛利率", "intent": "METRIC",
        # 只放财报原文必现的表述：多加"综合毛利率"这类同义写法会拉低 IDF 覆盖率，
        # 实测 top1 相关性从 0.46 掉到 0.256，直接被闸门判 low_score 转兜底。
        "keywords": ["毛利率", "营业毛利"], "prefer_table": True,
        "next": ["revenue", "expense_ratio"],
    },
    "operating_profit": {
        "label": "营业利润", "intent": "METRIC",
        "keywords": ["营业利润", "利润总额"], "prefer_table": True,
        "next": ["net_profit", "expense_ratio"],
    },
    "eps": {
        "label": "每股收益", "intent": "METRIC",
        "keywords": ["每股收益", "基本每股收益", "稀释每股收益"], "prefer_table": True,
        "next": ["net_profit", "roe"],
    },
    "roe": {
        "label": "净资产收益率", "intent": "METRIC",
        "keywords": ["净资产收益率", "加权平均净资产收益率", "ROE"], "prefer_table": True,
        "next": ["net_profit", "asset_liability"],
    },
    "expense_ratio": {
        "label": "期间费用", "intent": "METRIC",
        "keywords": ["销售费用", "管理费用", "财务费用", "期间费用"], "prefer_table": True,
        "next": ["net_profit", "gross_margin"],
    },
    "rd_expense": {
        "label": "研发投入", "intent": "METRIC",
        "keywords": ["研发投入", "研发费用", "研究与开发"], "prefer_table": True,
        "next": ["operating_profit", "core_competence"],
    },
    "nonrecurring": {
        "label": "非经常性损益", "intent": "METRIC",
        "keywords": ["非经常性损益", "扣除非经常性损益"], "prefer_table": True,
        "next": ["net_profit", "eps"],
    },
    # ---------- 现金流量 ----------
    "cashflow": {
        "label": "经营现金流", "intent": "METRIC",
        "keywords": ["经营活动产生的现金流量净额", "现金流"], "prefer_table": True,
        "next": ["net_profit", "capex"],
    },
    "capex": {
        "label": "资本开支", "intent": "METRIC",
        "keywords": ["购建固定资产", "资本开支", "投资活动现金流出"], "prefer_table": True,
        "next": ["cashflow", "strategy"],
    },
    # ---------- 资产负债 / 资产质量 ----------
    "total_assets": {
        "label": "总资产", "intent": "METRIC",
        "keywords": ["总资产", "资产总计"], "prefer_table": True,
        "next": ["asset_liability", "receivable"],
    },
    "asset_liability": {
        "label": "资产负债率", "intent": "METRIC",
        "keywords": ["资产负债率", "负债合计", "总负债"], "prefer_table": True,
        "next": ["total_assets", "cash_balance"],
    },
    "goodwill": {
        "label": "商誉", "intent": "METRIC",
        "keywords": ["商誉", "商誉减值"], "prefer_table": True,
        "next": ["total_assets", "risk"],
    },
    "receivable": {
        "label": "应收账款", "intent": "METRIC",
        "keywords": ["应收账款", "应收票据及应收账款"], "prefer_table": True,
        "next": ["cashflow", "total_assets"],
    },
    "inventory": {
        "label": "存货", "intent": "METRIC",
        "keywords": ["存货", "存货跌价"], "prefer_table": True,
        "next": ["cashflow", "business_mix"],
    },
    "cash_balance": {
        "label": "货币资金", "intent": "METRIC",
        "keywords": ["货币资金", "现金及现金等价物"], "prefer_table": True,
        "next": ["cashflow", "asset_liability"],
    },
    # ---------- 结构与治理（表格型） ----------
    "business_mix": {
        "label": "主营业务构成", "intent": "TABLE",
        "keywords": ["主营业务构成", "分行业", "分产品"], "prefer_table": True,
        "next": ["revenue", "gross_margin"],
    },
    "dividend": {
        "label": "分红方案", "intent": "TABLE",
        "keywords": ["利润分配", "现金分红", "每10股派"], "prefer_table": True,
        "next": ["net_profit", "cashflow"],
    },
    "shareholder": {
        "label": "股东情况", "intent": "TABLE",
        "keywords": ["前10名股东", "股东情况", "控股股东"], "prefer_table": True,
        "next": ["dividend", "strategy"],
    },
    "employee": {
        "label": "员工情况", "intent": "TABLE",
        "keywords": ["在职员工", "员工数量", "专业构成"], "prefer_table": True,
        "next": ["expense_ratio", "business_mix"],
    },
    # ---------- 定性 ----------
    "risk": {
        "label": "风险因素", "intent": "QUALITATIVE",
        "keywords": ["风险因素", "可能面对的风险"], "prefer_table": False,
        "next": ["strategy", "industry_pattern"],
    },
    "strategy": {
        "label": "经营战略", "intent": "QUALITATIVE",
        "keywords": ["经营战略", "发展战略", "未来发展的展望"], "prefer_table": False,
        "next": ["risk", "industry_pattern"],
    },
    "industry_pattern": {
        "label": "行业格局", "intent": "QUALITATIVE",
        "keywords": ["行业格局", "行业情况", "行业发展趋势"], "prefer_table": False,
        "next": ["core_competence", "strategy"],
    },
    "core_competence": {
        "label": "核心竞争力", "intent": "QUALITATIVE",
        "keywords": ["核心竞争力", "核心竞争优势"], "prefer_table": False,
        "next": ["strategy", "industry_pattern"],
    },
    "audit_opinion": {
        "label": "审计意见", "intent": "QUALITATIVE",
        "keywords": ["审计意见", "审计报告", "标准无保留意见"], "prefer_table": False,
        "next": ["risk", "receivable"],
    },
    "related_party": {
        "label": "关联交易", "intent": "QUALITATIVE",
        "keywords": ["关联交易", "关联方"], "prefer_table": True,
        "next": ["risk", "business_mix"],
    },
}

# ---------- 星图分组（前端渲染顺序）----------
# 字典里只有注释分组、不可机读，这里显式列出：前端按此顺序渲染，也便于埋点/配色。
# 新增字典节点时**必须**同步加到对应组，否则前端会漏渲染（有单测断言分组覆盖全部 id）。
STAR_GROUPS: List[Tuple[str, str, List[str]]] = [
    ("利润表 · 盈利能力", "gold", [
        "revenue", "net_profit", "gross_margin", "operating_profit",
        "eps", "roe", "expense_ratio", "rd_expense", "nonrecurring",
    ]),
    ("现金流量", "blue", ["cashflow", "capex"]),
    ("资产负债 · 资产质量", "blue", [
        "total_assets", "asset_liability", "goodwill", "receivable", "inventory", "cash_balance",
    ]),
    ("结构与治理", "green", ["business_mix", "dividend", "shareholder", "employee"]),
    ("定性披露", "green", [
        "risk", "strategy", "industry_pattern", "core_competence", "audit_opinion", "related_party",
    ]),
]

# ---------- 冷启动种子点 ----------
# 用户第一次进来时 unlocked 为空，若按"距离已点亮 ≤1"计算 clickable 会得到一个都不亮 ——
# 空星图没有任何可行动作，用户不知道能干什么。种子点是"任何一份 A 股定期报告都必然披露"的
# 科目，冷启动时直接可点，作为探索起点。
# 选词标准同字典：必须是**必有科目**。这里刻意只放 5 个且覆盖四个分组，
# 让用户第一眼就看到"这图讲的是什么结构"，而不是一堆按钮。
SEED_NODES: List[str] = ["revenue", "net_profit", "cashflow", "total_assets", "business_mix"]

# ---------- 能力未就绪的节点 ----------
# {node_id: 不可用原因}。这类节点可能已被规则边连进图，但**不参与 clickable**、
# **禁止连边**（v0.5：连了也点不亮，等能力就绪后会批量生效，用户早忘了自己连过，
# 突然看到它亮了还挂着一条边，无从解释）。
# 当前字典节点都是财报科目、均可解析，故为空；接入行情/估值后在此登记（如 pe / pb）。
UNAVAILABLE_NODES: Dict[str, str] = {}

# 允许从前端进入检索链路的过滤字段（其余一律丢弃）
# prefer_period：系统补的"默认期"，只进检索词不进过滤（区别于用户显式指定的 report_period）
# report_periods：对比模式的期间列表（>=2 即进入对比），做硬过滤
# via：本轮点击的来源（star | text_link | llm_candidate），决定邻接校验是否放行
# candidates_token：候选列表下发时刻的指纹，提交时校验，防止跨拓扑静默建边
# link_target / link_confirmed：连边的目标端与"用户已确认"标志（连边必须显式确认）
_UI_KEYS = ("node_id", "company", "stock_code", "report_period", "report_periods",
            "industry", "section_keywords", "prefer_table", "prefer_period", "compare",
            "via", "candidates_token", "current_node_id",
            "link_target", "link_confirmed",
            # 未选公司时的跨公司口径标记（可选上下文，不是前置门槛）
            "cross_company",
            # 新建节点：中文名 + 检索关键词（关键词必须能召回，否则不放行）
            "label", "keywords")

# 只有这些字段参与 chunk 硬过滤（company 仅用于展示/检索词，section 是软偏好）
_FILTER_KEYS = ("stock_code", "report_period", "report_periods", "industry")

_CODE = re.compile(r"\d{4,6}")

# 库外标的的哨兵代码：故意取一个库里不可能存在的值。
# 用途：前端点了"特斯拉"这类语料里没有的公司时，company 归一不出 stock_code，
# 于是三路召回没有硬过滤条件——向量路的 where 为 None，必然按语义召回别家公司的片段，
# 卡片就会拿"营业收入的通用定义"冒充成特斯拉的答案，且 refused=false。
# 注入哨兵后 filters 非空，配合 filters_strict 把三路打空 -> 转兜底（refused=true）。
OOS_STOCK_CODE = "__not_in_corpus__"

# 点击来源（via）枚举。决定邻接校验是否放行，见 `check_unlock_allowed`。
VIA_STAR = "star"                  # 点星图节点：必须经过邻接校验
VIA_TEXT_LINK = "text_link"        # 在正文/卡片里点已点亮节点的链接：放行（D2/D16 豁免）
VIA_LLM_CANDIDATE = "llm_candidate"  # 系统给的候选：放行（候选本身已满足可点亮性）
VIA_VALUES = (VIA_STAR, VIA_TEXT_LINK, VIA_LLM_CANDIDATE)

# 图上"不可达"的哨兵距离。用大整数而非 None，便于直接参与 min() 比较。
DIST_INF = 99


def _norm_code(v: Any) -> str:
    """公司字段归一：'腾讯(0700)' / '600519.SH' -> '0700' / '600519'。"""
    m = _CODE.search(str(v or ""))
    return m.group(0) if m else ""


def clean_ui_filters(raw: Any) -> Dict[str, Any]:
    """白名单清洗前端参数：脏 key、空值、非法类型一律丢弃。"""
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {}
    for k in _UI_KEYS:
        v = raw.get(k)
        if v in (None, "", [], {}):
            continue
        if k == "node_id":
            out["node_id"] = str(v).strip().lower()
        elif k == "stock_code":
            # 哨兵值必须原样放行：它不是真代码，而是"必然过滤落空"的信号。
            # 若在这里被 _norm_code 判为非法而丢弃，库外标的就退回无过滤状态——
            # 向量路的 where 变回 None，又会按语义召回别家公司的片段，
            # 把营业收入的通用定义冒充成该公司的答案（refused 还是 false）。
            if str(v) == OOS_STOCK_CODE:
                out["stock_code"] = OOS_STOCK_CODE
            else:
                code = _norm_code(v)
                if code:
                    out["stock_code"] = code
        elif k == "company":
            code = _norm_code(v)
            if code:                          # "0700" / "600519" 归一成可过滤维度
                out["stock_code"] = code
            out["company"] = str(v).strip()   # 原样保留，供检索词与 UI 展示使用
        elif k == "report_period":
            out["report_period"] = str(v).strip().upper()
        elif k == "report_periods":
            # 对比期间：最多两期（更多期当前卡片模板放不下，且会让检索词稀释）。
            # 去重后按原顺序保留，方便前端保序渲染。
            ps: List[str] = []
            for x in (v if isinstance(v, (list, tuple)) else [v]):
                p = str(x).strip().upper()
                if p and p not in ps:
                    ps.append(p)
            if ps:
                out["report_periods"] = ps[:2]
        elif k == "prefer_period":
            # 不在 _FILTER_KEYS 里：只参与 star_query 的检索词，不做硬过滤
            out["prefer_period"] = str(v).strip().upper()
        elif k == "industry":
            out["industry"] = str(v).strip()
        elif k == "section_keywords":
            out["section_keywords"] = [str(x) for x in v][:5]
        elif k == "label":
            out["label"] = str(v).strip()[:20]
        elif k == "keywords":
            # 新建节点的检索锚点：最多 3 个（与 schema 一致），空串丢弃
            ks: List[str] = []
            for x in (v if isinstance(v, (list, tuple)) else [v]):
                s = str(x).strip()
                if s and s not in ks:
                    ks.append(s)
            if ks:
                out["keywords"] = ks[:3]
        elif k == "prefer_table":
            out["prefer_table"] = bool(v)
        elif k == "compare":
            # 只要对比、不指定哪两期时，由 _resolve_scope 补"最新 + 去年同期"
            out["compare"] = bool(v)
        elif k == "via":
            # 来源必须枚举收敛：未知值一律丢弃，让邻接校验按默认的严格档处理，
            # 而不是被前端传个 "admin" 之类就绕过校验。
            v = str(v).strip().lower()
            if v in VIA_VALUES:
                out["via"] = v
        elif k == "candidates_token":
            out["candidates_token"] = str(v).strip()[:64]
        elif k == "current_node_id":
            out["current_node_id"] = str(v).strip().lower()
        elif k == "link_target":
            out["link_target"] = str(v).strip().lower()
        elif k == "link_confirmed":
            out["link_confirmed"] = bool(v)
    return out


def to_filters(ui_filters: Dict[str, Any]) -> Dict[str, Any]:
    """ui_filters -> 召回用的过滤条件（node_id 不参与 chunk 过滤，只用于定位节点）。"""
    return {k: v for k, v in (ui_filters or {}).items() if k in _FILTER_KEYS}


def node_meta(node_id: str, session_nodes: Dict[str, Any] = None) -> Dict[str, Any]:
    """取节点定义：全局字典优先，其次是本会话验证通过的新节点。

    会话节点与字典节点对下游完全同构（都有 label / keywords / intent），
    所以 star_query / label / intent 三处只需改这一处查找，不必各写一套分支。
    """
    m = STAR_NODES.get(node_id or "")
    if m:
        return m
    m = (session_nodes or {}).get(node_id or "")
    return m if isinstance(m, dict) else {}


def known_node_ids(session_nodes: Dict[str, Any] = None) -> set:
    """当前可渲染的节点 id 全集：字典 + 会话已验证节点。"""
    return set(STAR_NODES) | set((session_nodes or {}).keys())


def node_is_draft(node_id: str, session_nodes: Dict[str, Any] = None) -> bool:
    """内容待补的占位节点（用户先建了个空节点，比如只填了公司名）。

    它**在图上可见**（用户建的东西必须看得见），但不可点亮、不进推荐位：
    空节点没有关键词，点亮它会拿 node_id 去召回一堆不相干的片段——
    那正是"造词幽灵节点"的老毛病。它只能等补了内容再点亮。
    """
    m = (session_nodes or {}).get(str(node_id or ""))
    return isinstance(m, dict) and str(m.get("content_status") or "") == DRAFT_STATUS


def star_query(node_id: str, ui_filters: Dict[str, Any],
               session_nodes: Dict[str, Any] = None) -> str:
    """星图节点 -> 检索关键词串，三路召回共用（保证三路条件一致）。

    prefer_period 是系统补的"默认期"，只拼进检索词做软偏好；
    report_period 是用户显式指定的，由 to_filters 做硬过滤。
    两者都参与检索词，但只有后者会真的把召回范围卡死。
    """
    node = node_meta(node_id, session_nodes)
    parts: List[str] = []
    if ui_filters.get("company"):
        parts.append(str(ui_filters["company"]))
    parts += list(node.get("keywords") or [node_id or ""])
    period = ui_filters.get("report_period") or ui_filters.get("prefer_period")
    if period:
        parts.append(_period_cn(str(period)))
    # 只有"确实存在构成表"的科目才拼这两个词。总资产 / 资产负债率 / 商誉这类单一
    # 数值科目没有构成表，硬拼进检索词只会拉低 IDF 覆盖率（实测把总资产从 0.28 拉到
    # 0.26，直接被闸门判 low_score 转兜底）。表格片段的加权交给 rerank 的 prefer_table
    # 处理，不靠检索词硬凑。
    if node.get("intent") == "TABLE":
        parts.append("明细 构成")
    return " ".join(dict.fromkeys([p for p in parts if p]))


def intent_for_node(node_id: str, session_nodes: Dict[str, Any] = None) -> str:
    return str(node_meta(node_id, session_nodes).get("intent") or "METRIC")


def label_for_node(node_id: str, session_nodes: Dict[str, Any] = None) -> str:
    return str(node_meta(node_id, session_nodes).get("label") or node_id or "")


def next_candidates(node_id: str, unlocked: List[str] = None) -> List[str]:
    """渐进式披露：本节点的推荐后继（排除已点亮，最多 2 个）。"""
    unlocked = set(unlocked or [])
    cands = [n for n in (STAR_NODES.get(node_id or "", {}).get("next") or []) if n not in unlocked]
    return cands[:2]


def clean_unlock_next(
    cands: Any,
    current: str = "",
    unlocked: List[str] = None,
    limit: int = 2,
    session_nodes: Dict[str, Any] = None,
) -> List[str]:
    """LLM 给的推荐必须落在星图白名单内，否则前端会渲染出点不动的幽灵节点。

    白名单 = 全局字典 + 本会话已通过探针的节点。模型**提议**的新节点不在这里放行：
    它们必须先过 `probe_node`，过了才写进 session_nodes，之后才可能被推荐。
    """
    unlocked = set(unlocked or [])
    # 草稿节点不能进推荐位：推荐位的价值是"点开就有内容"，推荐一个空节点等于骗一次点击
    allowed = {n for n in known_node_ids(session_nodes)
               if not node_is_draft(n, session_nodes)}
    out: List[str] = []
    for c in cands or []:
        c = str(c).strip().lower()
        if c in allowed and c != current and c not in unlocked and c not in out:
            out.append(c)
        if len(out) >= limit:
            break
    return out


def _period_cn(period: str) -> str:
    p = (period or "").strip().upper()
    # 尾缀必须按"年份之后"取：p[-2:] 对 "2025A" 取到 "5A"（年度期永远匹配不上，
    # 于是 "2025A" 原样进检索词，中文年份表述丢失，召回明显变差），对 "2025H1" 才碰巧正确。
    year, tail = p[:4], p[4:]
    return {"H1": "%s年半年度" % year, "A": "%s年年度" % year,
            "Q1": "%s年一季度" % year, "Q3": "%s年三季度" % year}.get(tail, p)


# 期间时间序：Q1 < H1 < Q3 < A（同一年内）。纯字符串排序会把 2025H1 排在 2025A 之后，
# 而实际上半年报早于年报——对比卡片上两期的先后顺序就会反。
_PERIOD_ORDER = {"Q1": 0, "H1": 1, "Q3": 2, "A": 3}


def _period_sort_key(period: str) -> Tuple[str, int]:
    p = str(period or "").strip().upper()
    return (p[:4], _PERIOD_ORDER.get(p[4:], 9))


def period_cn(period: str) -> str:
    """报告期 -> 中文（'2025H1' -> '2025年半年度'），供卡片文案与提示词展示。"""
    return _period_cn(period)


def scope_of_chunks(ctx: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从召回片段反推"这批资料属于哪家公司、哪一期报告"。

    为什么不直接读 filters：filters 可能是空的，也可能被注入 OOS 哨兵；
    真正喂给模型的是 ctx，用它反推不会说谎。取出现次数最多的组合，
    偶发混入的异类片段（硬过滤漏网）不会带偏主体。

    用途：页码只在「公司 + 期间」下才唯一——同一家有 6 期报告，P12 每期都存在。
    前端要把 citations 渲染成"贵州茅台 2025年半年度 P12"就必须带这两个坐标。
    """
    counts: Dict[Tuple[str, str, str], int] = {}
    for c in ctx or []:
        key = (
            str(c.get("stock_code") or ""),
            str(c.get("report_period") or ""),
            str(c.get("short_name") or c.get("company") or ""),
        )
        if not key[0] and not key[2]:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return {}
    (code, period, name), _ = max(counts.items(), key=lambda kv: kv[1])
    # 对比时要给出**全部**涉及期间：只回一个 report_period 的话，两期都有 P54 时
    # 前端无法判断该页码属于哪一期，引用就指不清了（引用按 公司-期间-页码 渲染）。
    per_counts: Dict[str, int] = {}
    for c in ctx or []:
        if code and str(c.get("stock_code") or "") != code:
            continue
        p = str(c.get("report_period") or "")
        if p:
            per_counts[p] = per_counts.get(p, 0) + 1
    # 按时间升序（早 -> 晚）：同比习惯是"2024 vs 2025"，按出现次数排会让两期顺序随机翻转
    periods = sorted(per_counts, key=_period_sort_key)[:2]
    return {
        "stock_code": code, "company": name, "report_period": period,
        "report_periods": periods or ([period] if period else []),
    }


# ============================================================================
# 知识星图拓扑与亮度（v0.5）
#
# 全部是纯函数：无 IO、无 LLM、不读全局可变状态（除 STAR_NODES / SEED_NODES 常量表），
# 因此前端与后端可以共用同一套定义 —— 否则"前端算一遍可点亮、后端算一遍"必然漂移，
# 用户会看到点了没反应的节点。
# ============================================================================

def _cfg():
    """延迟导入配置：ui 模块被导入时不触发 .env 读取与目录创建（保持"纯函数层"）。"""
    from .config import CONFIG
    return CONFIG


def is_available(node_id: str) -> bool:
    """节点能力是否就绪。未就绪者不参与 clickable、禁止连边。"""
    return str(node_id or "") not in UNAVAILABLE_NODES


def unavailable_reason(node_id: str) -> str:
    """不可用的原因文案（供卡片展示）。可用时返回空串。"""
    return UNAVAILABLE_NODES.get(str(node_id or ""), "")


def _adjacency(session_edges: List[Dict[str, Any]] = None,
               session_nodes: Dict[str, Any] = None) -> Dict[str, List[Tuple[str, bool]]]:
    """无向邻接表 {node: [(neighbor, is_session_edge), ...]}。

    规则边取 `next` 的**无向对称闭包**（D8）：A.next 含 B 不代表 B.next 含 A，
    但图上二者相邻。不做闭包的话"从 B 走回 A"会算成不可达，距离随方向变化，
    同一对节点因点击先后得到不同的可点亮集合 —— 用户会觉得星图在随机变。
    """
    adj: Dict[str, List[Tuple[str, bool]]] = {}
    ids = known_node_ids(session_nodes)

    def _add(a: str, b: str, sess: bool) -> None:
        a, b = str(a or ""), str(b or "")
        if a not in ids or b not in ids or a == b:
            return
        adj.setdefault(a, [])
        adj.setdefault(b, [])
        if (b, sess) not in adj[a]:
            adj[a].append((b, sess))
        if (a, sess) not in adj[b]:
            adj[b].append((a, sess))

    for nid in ids:
        for nb in (node_meta(nid, session_nodes).get("next") or []):
            _add(nid, nb, False)
    for e in session_edges or []:
        if isinstance(e, dict):
            _add(e.get("from") or e.get("a"), e.get("to") or e.get("b"), True)
    return adj


def _shortest_path(src: str, dst: str, adj: Dict[str, List[Tuple[str, bool]]],
                   max_session_edges: int = 1) -> Optional[List[str]]:
    """最短路径（含两端）；不可达返回 None。

    BFS 状态是 (节点, 已用会话边数)，因此"用了 1 条会话边"与"没用"是两个不同状态，
    天然阻止会话边串联。

    v0.5 · D19：会话边若可自由串联，几十个节点的图里连几条边就能把直径压到 2~3，
    `clickable ≈ 全部`，「探索前沿」这个核心机制自我瓦解 —— 而用户恰恰有动机这么做
    （能连边何必一步步走）。限制「路径中最多 1 条会话边」后：
    单条边仍能推开 1 跳（功能保住），多条边不会累加成隧道（破坏力被限住）。
    """
    from collections import deque

    if not src or not dst:
        return None
    if src == dst:
        return [src]
    q = deque([(src, 0)])
    parent: Dict[Tuple[str, int], Tuple[str, int]] = {}
    seen = {(src, 0)}
    while q:
        node, used = q.popleft()
        for nb, sess in adj.get(node, ()):
            nu = used + (1 if sess else 0)
            if nu > max_session_edges or (nb, nu) in seen:
                continue
            seen.add((nb, nu))
            parent[(nb, nu)] = (node, used)
            if nb == dst:
                path, cur = [dst], (nb, nu)
                while cur != (src, 0):
                    cur = parent[cur]
                    path.append(cur[0])
                return list(reversed(path))
            q.append((nb, nu))
    return None


def dist_G(src: str, dst: str, session_edges: List[Dict[str, Any]] = None,
           session_nodes: Dict[str, Any] = None, max_session_edges: int = 1) -> int:
    """图上距离（跳数）。不可达返回 DIST_INF。src == dst 返回 0。"""
    if not src or not dst:
        return DIST_INF
    if src == dst:
        return 0
    p = _shortest_path(src, dst, _adjacency(session_edges, session_nodes), max_session_edges)
    return len(p) - 1 if p else DIST_INF


def _clickable_node(node_id: str, session_nodes: Dict[str, Any] = None) -> bool:
    """节点本身是否具备被点亮的条件（可用 + 不是草稿）。"""
    return is_available(node_id) and not node_is_draft(node_id, session_nodes)


def compute_clickable(unlocked: List[str] = None,
                      session_edges: List[Dict[str, Any]] = None,
                      session_nodes: Dict[str, Any] = None) -> List[str]:
    """当前可点亮的节点集合 —— 「探索前沿」。

    规则：未点亮节点 x 可点亮 ⟺ ∃ 已点亮节点 u 使 dist_G(x, u) ≤ 1（即相邻）。

    冷启动（unlocked 为空）时退回 SEED_NODES：否则星图一个亮点都没有，
    用户面对全灰的图不知道能干什么 —— 这是"渐进式披露"唯一的例外，且只在首次发生。
    """
    ids = known_node_ids(session_nodes)
    unlocked = [n for n in (unlocked or []) if n in ids]
    if not unlocked:
        out = [n for n in SEED_NODES if n in ids and _clickable_node(n, session_nodes)]
        # 冷启动但用户**已经连过边**时，边的两端必须可点：那是他自己连出来的关联，
        # 看得见却点不动、还被告知"还没解锁"，是无从解释的自相矛盾。
        # 连边的全部价值就是推开前沿，冷启动下若失效，会话边就成了装饰品。
        for e in session_edges or []:
            if not isinstance(e, dict):
                continue
            for k in ("from", "to"):
                nid = str(e.get(k) or "")
                if nid in ids and _clickable_node(nid, session_nodes) and nid not in out:
                    out.append(nid)
        return out
    unlocked_set = set(unlocked)
    adj = _adjacency(session_edges, session_nodes)
    out: List[str] = []
    for nid in sorted(ids):
        if nid in unlocked_set or not _clickable_node(nid, session_nodes):
            continue
        if any(nb in unlocked_set for nb, _ in adj.get(nid, ())):
            out.append(nid)
    return out


def next_hop_to(target: str, unlocked: List[str] = None,
                session_edges: List[Dict[str, Any]] = None,
                session_nodes: Dict[str, Any] = None) -> Optional[str]:
    """从已点亮区域走向 target 的**下一跳**节点（该节点本身必须可点亮）。

    locked 卡片上要显示「先看 X」，不能只说"不可点亮"—— 那等于把用户丢在原地。
    返回 None 表示 target 已可点亮、或根本不可达。
    """
    ids = known_node_ids(session_nodes)
    unlocked = [n for n in (unlocked or []) if n in ids]
    if not unlocked or not target or target not in ids:
        return None
    adj = _adjacency(session_edges, session_nodes)
    best: Optional[List[str]] = None
    for u in unlocked:
        p = _shortest_path(target, u, adj)
        if p and (best is None or len(p) < len(best)):
            best = p
    if not best or len(best) < 2:
        return None
    # best = [target, ..., hop, u]：hop 是紧挨已点亮区的那一步
    hop = best[-2]
    return None if hop in set(unlocked) else hop


def node_brightness(node_id: str, heat: Dict[str, float] = None,
                    last_seen: Dict[str, float] = None, now: float = None) -> float:
    """亮度 = 热度 × 时间衰减（wall clock，D6）。

    热度是**累加量**（只增），本身表达不了"多久没看了"；乘上按真实时间半衰的因子后，
    "上周看过的节点"自然比"昨天看过的"暗，与用户直觉一致。
    """
    cfg = _cfg()
    now = float(now if now is not None else time.time())
    try:
        h = float((heat or {}).get(node_id, 0.0))
    except (TypeError, ValueError):
        return 0.0
    if h <= 0:
        return 0.0
    h = min(h, cfg.heat_cap)
    try:
        ts = float((last_seen or {}).get(node_id, 0.0))
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        return h
    elapsed = max(0.0, now - ts)
    return h * (0.5 ** (elapsed / cfg.decay_half_life_sec))


def node_brightness_tier(unlocked: List[str] = None, heat: Dict[str, float] = None,
                         last_seen: Dict[str, float] = None,
                         now: float = None) -> Dict[str, str]:
    """把已点亮节点切成 bright / mid / dim 三档（v0.5 口径）。

    v0.4 用"按比例切档"（前 25% / 后 25%），有两个致命缺陷：
      ① **跳变** —— 用户点亮 A，B 就从 bright 掉到 mid，而用户对 B 什么都没做；
      ② **虚假差异** —— 衰退按 wall clock，用户一周后回来所有值都趋近 0，
         但**排名永远存在**，必然有 25% 被标成"最近常看"，与事实相反。
    根因是相对分档没有绝对锚点。v0.5 改为「绝对数量锚定 + 绝对门限前置」：

      1. max(brightness) < tier_min_brightness → **不分档**，全部 dim
         （堵住"所有节点都很暗、却硬有几个被标成最亮"的荒谬态）；
      2. 分档只按 last_seen 排序，不按 brightness 排名 —— 总数增长时只有真正的
         "最近 N 个 / 最久 N 个"会变，**其他节点永不跳档**；
      3. 数量 ≤ tier_min_nodes 时全部 mid（否则 bright 与 dim 会重叠，分档自相矛盾）。
    """
    cfg = _cfg()
    now = float(now if now is not None else time.time())
    unlocked = [n for n in (unlocked or []) if n]
    if not unlocked:
        return {}
    vals = {n: node_brightness(n, heat, last_seen, now) for n in unlocked}
    if max(vals.values()) < cfg.tier_min_brightness:
        return {n: "dim" for n in unlocked}
    if len(unlocked) <= cfg.tier_min_nodes:
        return {n: "mid" for n in unlocked}

    # 只按 last_seen 排序；并列用 node_id 字典序破平，保证同一输入输出稳定（不抖动）
    by_seen = sorted(unlocked, key=lambda n: (float((last_seen or {}).get(n, 0.0)), n))
    tiers = {n: "mid" for n in unlocked}
    # 暗档先落：数量不足时 bright 与 dim 可能重叠，重叠处让"最近"优先（用户更关心最近）
    for n in by_seen[: cfg.tier_dim_n]:
        tiers[n] = "dim"
    for n in by_seen[-cfg.tier_bright_n:]:
        tiers[n] = "bright"
    return tiers


def apply_pulse(tier: str, t_recommend: float = None, now: float = None) -> str:
    """引导脉冲：在档位之上**临时提升一档**，持续 PULSE_DECAY_SEC 后回落。

    v0.5 修订：v0.4 写的是 brightness + pulse（连续量），但渲染只认离散档位，两者
    没有合并规则，且亮度由服务端下发 —— 计算位置自相矛盾。改为提档后，前端只需持有
    t_recommend 时间戳即可自行渲染，**不需要服务端参与**，也绝不写回 heat。
    """
    cfg = _cfg()
    now = float(now if now is not None else time.time())
    # 必须是 is None 判断：0.0 是合法时间戳，写成 `if not t_recommend` 会把它当"未提供"，
    # 脉冲永远不生效。
    if t_recommend is None:
        return tier
    if now - float(t_recommend) >= cfg.pulse_decay_sec:
        return tier
    _NEXT = {"dim": "mid", "mid": "bright", "bright": "bright"}
    return _NEXT.get(tier, tier)


def match_nodes_by_query(query: str, session_nodes: Dict[str, Any] = None,
                         limit: int = 2) -> List[str]:
    """自然语言 -> 星图节点（按在 query 中出现的位置排序）。

    用于模式一"毛利率和净利润有什么关系"这类口述连边：需要同时解析出**两个**节点。
    匹配优先级：node_id 全等 > label 全等 > label 被 query 包含（长者优先）> 关键词包含。
    "长者优先"是刻意的：查"净利润"时不应命中"净利"，反之亦然。
    """
    q = str(query or "").strip().lower()
    if not q:
        return []
    ids = sorted(known_node_ids(session_nodes))

    hits: List[Tuple[int, int, str]] = []   # (位置, 匹配长度, node_id)

    def _push(pos: int, length: int, nid: str) -> None:
        if nid not in [h[2] for h in hits]:
            hits.append((pos, length, nid))

    for nid in ids:
        if nid == q:
            _push(0, len(nid), nid)
        lab = label_for_node(nid, session_nodes).lower()
        if lab and lab == q:
            _push(0, len(lab), nid)
    if not hits:
        for nid in ids:
            lab = label_for_node(nid, session_nodes).lower()
            if lab and lab in q:
                _push(q.find(lab), -len(lab), nid)
    if not hits:
        for nid in ids:
            for kw in (node_meta(nid, session_nodes).get("keywords") or []):
                kw = str(kw).strip().lower()
                if len(kw) >= 2 and kw in q:
                    _push(q.find(kw), -len(kw), nid)
                    break
    hits.sort(key=lambda h: (h[0], h[1]))
    return [h[2] for h in hits[:limit]]


def match_node_by_query(query: str, session_nodes: Dict[str, Any] = None) -> Optional[str]:
    """单节点匹配（match_nodes_by_query 的单返回值版本）。"""
    m = match_nodes_by_query(query, session_nodes, limit=1)
    return m[0] if m else None


def check_unlock_allowed(node_id: str, unlocked: List[str] = None,
                         session_edges: List[Dict[str, Any]] = None,
                         session_nodes: Dict[str, Any] = None,
                         via: str = "") -> Tuple[bool, str, Optional[str]]:
    """是否允许点亮该节点。返回 (allowed, reason, next_hop)。

    邻接校验只约束**点星图**这条路径（via=star）；正文链接与系统候选放行：
      - via=text_link（D2/D16 豁免）：用户在卡片正文里点了某个节点的链接，
        那是系统自己给的路径，再判"不相邻"就是把用户刚看到的东西又锁上；
      - via=llm_candidate：候选本身已由拓扑筛过。
    """
    ids = known_node_ids(session_nodes)
    nid = str(node_id or "")
    if nid not in ids:
        return False, "unknown_node", None
    # 草稿节点优先于下面所有放行规则（包括"会话节点一律放行"）：
    # 它没有关键词，点亮只会召回不相干的片段，必须先补内容。
    if node_is_draft(nid, session_nodes):
        return False, "draft", None
    if not is_available(nid):
        return False, "unavailable", None
    unlocked = [n for n in (unlocked or []) if n in ids]
    if nid in unlocked:
        return True, "", None
    # 会话节点（模型提议、经可召回性探针验证后造出来的）一律放行。
    # 它是用户**提问**问出来的，问本身就已经表达了"我想看"；若再要求它与已点亮节点
    # 相邻，就会造出"问出来却永远点不亮"的节点 —— 这正是 v0.4 僵尸节点问题的翻版：
    # 星图上多了个点，却永远打不开。
    if nid in (session_nodes or {}):
        return True, "", None
    if via in (VIA_TEXT_LINK, VIA_LLM_CANDIDATE):
        return True, "", None
    if not unlocked:
        # 冷启动：只有种子点可点
        return (True, "", None) if nid in SEED_NODES else (False, "not_frontier", None)
    adj = _adjacency(session_edges, session_nodes)
    unlocked_set = set(unlocked)
    if any(nb in unlocked_set for nb, _ in adj.get(nid, ())):
        return True, "", None
    return False, "not_frontier", next_hop_to(nid, unlocked, session_edges, session_nodes)
