"""Graph 状态定义（Python 3.9 兼容：不用 list[str] / X | None）。

约定：
- 节点统一签名 (state) -> dict，返回**增量 patch**，不直接 mutate 大对象。
- errors / degraded / trace 使用 add_list reducer 累加；其余字段为覆盖写。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from typing_extensions import Annotated, TypedDict


# 新回合哨兵：只作为 reducer 的控制信号，不会留在 state 里
_RESET = "__vc_reset__"
RESET_LIST: List[Any] = [_RESET]
# 字典型字段的同款哨兵（node_heat 用）：list 版塞进 dict reducer 会被 dict() 拆失败，
# 所以单独给一个 dict 形态，语义一致——清空后按本轮内容重建。
RESET_DICT: Dict[str, Any] = {_RESET: 1.0}


def add_list(a: List[Any], b: List[Any]) -> List[Any]:
    """累加型 reducer：只增不减，保证错误与 trace 不被后续节点覆盖。

    新回合哨兵：Checkpointer 会让 errors/trace 在同一 thread_id 下跨轮累积（越滚越长），
    因此每轮第一个节点（query_rewrite）返回 RESET_LIST 表示"本轮重新计数"；
    哨兵本身会被下面的过滤去掉，不会污染 fallback_node 对 errors 的扫描。
    语义仍是"只增不减"——只是把窗口从"整个会话"收敛到"当前轮次"。
    """
    b = list(b or [])
    if _RESET in b:
        return [x for x in b if x != _RESET]
    return (a or []) + b


def merge_unique(a: List[str], b: List[str]) -> List[str]:
    """去重累加：unlocked_nodes 跨轮次只增不重复，顺序稳定（先点亮者在前）。

    哨兵 RESET_LIST 表示"清空后按本轮内容重建"（前端 reset_starmap 用），
    否则 reducer 只增不减、无法清空。
    """
    b = list(b or [])
    if _RESET in b:
        return [x for x in b if x != _RESET]
    out = list(a or [])
    seen = set(out)
    for x in b or []:
        x = str(x)
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def merge_heat(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    """节点热度累加：直接点击 +1.0，被间接关联（出现在别节点的 unlock_next 里）+0.5。

    与 `unlocked_nodes` 的分工：
    - `unlocked_nodes` 是布尔集合，语义是"这个节点的内容用户确实读过"，
      所以它只由直接点击产生——否则星图会出现"没读过却已亮"的节点，
      用户点进去发现没内容；
    - `node_heat` 是连续量，语义是"用户与该节点的关联强度"，星图亮度按它渲染。

    热度**不随公司/期间切换重置**：它衡量的是用户兴趣的累积，与当前作用域无关。
    哨兵 RESET_LIST 仍可清空（前端 reset_starmap 用），语义与 merge_unique 一致。
    """
    b = dict(b or {})
    if b.pop(_RESET, None) is not None:
        return {k: float(v) for k, v in b.items() if k != _RESET}
    out = dict(a or {})
    for k, v in b.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f:
            out[str(k)] = round(out.get(str(k), 0.0) + f, 3)
    return out


def merge_nodes(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """会话星图节点累加：模型造的新节点**通过可召回性探针**后才写入这里。

    为什么用覆盖而不是累加：值是节点定义（label / keywords），同一个 id 再次出现时
    应取最新的（模型可能在后续轮次给出更准的 keywords），dict 相加没有意义。

    为什么只活在会话里、不写进 `STAR_NODES`：全局字典是人工确认过的"财报必有科目"，
    模型一次抖动就把词塞进去会永久污染；会话级则随会话结束自然消失，风险可控。
    """
    b = dict(b or {})
    if b.pop(_RESET, None) is not None:
        return {k: v for k, v in b.items() if isinstance(v, dict) and v}
    out = dict(a or {})
    for k, v in b.items():
        if isinstance(v, dict) and v:
            out[str(k)] = dict(v)
    return out


def merge_last_seen(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    """节点最近点亮时间 {node_id: epoch}：同一节点取更大的时间戳。

    为什么单独存、不从 node_heat 推：heat 是**累加量**（只增），无法表达"最近一次是什么时候"。
    而 v0.5 的分档只按 last_seen 排序（§5.5），必须有一个独立的时间维度。
    重新点亮会刷新它 —— 这正是"最近 3 个"语义的来源。

    哨兵 RESET_DICT 语义同 merge_heat：清空后按本轮内容重建。
    """
    b = dict(b or {})
    if b.pop(_RESET, None) is not None:
        return {str(k): float(v) for k, v in b.items() if k != _RESET}
    out = dict(a or {})
    for k, v in b.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > out.get(str(k), 0.0):
            out[str(k)] = f
    return out


def _edge_key(edge: Any) -> Optional[Tuple[str, str]]:
    """无向边归一化键：("a","b") 与 ("b","a") 视为同一条（与 D8 无向闭包一致）。

    非法边（不是 dict / 缺端点）返回 None，交由调用方丢弃——
    返回空元组会让 merge_edges 把它们都归到同一个键下互相覆盖。
    """
    if not isinstance(edge, dict):
        return None
    a = str(edge.get("from") or edge.get("a") or "").strip().lower()
    b = str(edge.get("to") or edge.get("b") or "").strip().lower()
    if not a or not b:
        return None
    return (a, b) if a <= b else (b, a)


def merge_edges(a: List[Any], b: List[Any]) -> List[Any]:
    """会话边累加：按无向键去重，受 MAX_SESSION_EDGES 上限截断。

    为什么顶层而非挂在 session_nodes[id] 下：模式一（用户口述两端）可能两端都是字典节点，
    无处可挂。

    哨兵 RESET_LIST 语义同 merge_unique：前端 reset 用。
    """
    b = list(b or [])
    if _RESET in b:
        return [x for x in b if x != _RESET]
    from .config import CONFIG  # 局部导入：避免 config 与 state 形成模块级循环

    out: Dict[Tuple[str, str], Any] = {}
    for e in (a or []) + b:
        k = _edge_key(e)
        if k:
            out.setdefault(k, e)
    return list(out.values())[: CONFIG.max_session_edges]


class Page(TypedDict, total=False):
    doc_id: str
    page: int
    text: str
    page_hash: str


class Chunk(TypedDict, total=False):
    chunk_id: str
    doc_id: str
    doc_version: str
    company: str
    short_name: str
    stock_code: str
    report_period: str
    report_type: str
    industry: str
    page: int
    section_path: str
    table_flag: bool
    text: str
    content_hash: str
    source_path: str
    source_url: str
    ingest_time: str
    embedding_provider: str
    embed_dim: int
    # 运行期打分（不入库）
    score: float
    source_route: str


class Citation(TypedDict, total=False):
    page: int
    doc_id: str
    snippet: str
    chunk_id: str


class GraphState(TypedDict, total=False):
    # ---------- 输入 ----------
    query_raw: str
    history: List[Dict[str, str]]

    # ---------- UI 事件（前端富交互载荷）----------
    ui_action: str            # click_star / natural_query / clarify_followup / reset_starmap
    ui_filters: Dict[str, Any]  # {"node_id": "gross_margin", "company": "0700", ...}
    star_node_id: str         # 本轮点亮的星图节点（由 ui_filters 归一化而来）
    filters_strict: bool      # True = ui_filters 是绝对过滤条件，召回为空也不回退
    ui_payload: Dict[str, Any]  # 回给前端的结构化指令（explanation/citations/unlock_next）
    # 已点亮节点（直接点击过）：靠 Checkpointer 跨轮持久化，reducer 保证只增不重复
    unlocked_nodes: Annotated[List[str], merge_unique]
    # 节点热度 {node_id: 权重}：直接点击 1.0、被间接关联 0.5，累加不因切换作用域而清零。
    # 星图亮度由它驱动（渲染规则待正式交互设计定，这里只保证数据可用）。
    node_heat: Annotated[Dict[str, float], merge_heat]
    # 节点最近点亮时间 {node_id: epoch 秒}。v0.5 分档只按它排序（不按 heat 排名），
    # 这样"点亮新节点"不会让无关节点跳档。
    node_last_seen: Annotated[Dict[str, float], merge_last_seen]
    # 会话边（用户主动连的关联）[{from,to,origin,created_at}]，无向、上限 MAX_SESSION_EDGES。
    session_edges: Annotated[List[Dict[str, Any]], merge_edges]
    # 会话星图：模型提议、且经可召回性探针验证通过的新节点 {node_id: {label, keywords}}。
    # 只在本会话有效，不写进 STAR_NODES 全局字典（那会永久污染，且无法回滚）。
    session_nodes: Annotated[Dict[str, Any], merge_nodes]
    # 会话作用域：本次会话锁定的公司（stock_code）。星图点击未带 company 时继承它，
    # 靠 Checkpointer 跨轮保留——先问"茅台净利润"再点星图，节点应当仍是茅台的数据。
    session_company: str

    # ---------- 改写与路由 ----------
    query_rewritten: str
    query_terms: List[str]
    filters: Dict[str, Any]
    intent: str
    intent_confidence: float
    intent_scores: Dict[str, float]
    route_cfg: Dict[str, Any]

    # ---------- 召回与融合 ----------
    recall_bm25: List[Chunk]
    recall_vector: List[Chunk]
    recall_meta: List[Chunk]
    fused: List[Chunk]
    reranked: List[Chunk]
    context: List[Chunk]
    fusion_stats: Dict[str, Any]

    # ---------- 生成与校验 ----------
    generation_used: List[str]     # generator 实际引用的 chunk_id，供 UI 溯源
    draft_answer: str
    answer: str
    citations: List[Dict[str, Any]]
    gate_pass: bool
    gate_reason: str
    valid: bool
    final_answer: str
    suggestions: List[str]
    fallback_reason: str

    # ---------- 横切 ----------
    errors: Annotated[List[Dict[str, Any]], add_list]
    degraded: Annotated[List[str], add_list]
    trace: Annotated[List[Dict[str, Any]], add_list]
    retry_count: int
    trace_id: str


class IngestState(TypedDict, total=False):
    doc_path: str
    force_rebuild: bool
    skip_bm25: bool          # 批量入库：逐文档跳过 BM25 重建，末尾统一重建一次
    doc_id: str
    doc_version: str
    doc_meta: Dict[str, Any]
    pages: List[Page]
    pages_clean: List[Page]
    chunks: List[Chunk]
    doc_hash: str
    diff_plan: Dict[str, Any]
    upsert_stats: Dict[str, Any]
    bm25_stats: Dict[str, Any]
    purge_stats: Dict[str, Any]
    manifest: Dict[str, Any]
    skipped: bool

    errors: Annotated[List[Dict[str, Any]], add_list]
    degraded: Annotated[List[str], add_list]
    trace: Annotated[List[Dict[str, Any]], add_list]
    trace_id: str
