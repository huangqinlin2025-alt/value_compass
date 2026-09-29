"""问答主图编排。

流程：
query_rewrite -> intent_router ->[Send 并行]-> 三路召回 -> rrf_fusion
  -> (空/低分 -> fallback | 正常 -> rerank -> compress -> generate -> gate)
  -> gate 失败可收紧重试 1 次 -> citation_validate -> unlock_commit -> output_guard -> END

富交互（知识星图）通道：
- ui_action=click_star / clarify_followup 时，query_rewrite 与 intent_router 双双短路，
  直接用 ui_filters 驱动三路召回（跳过大模型 JSON 解析，也绕过空的 query_raw）；
- generator 在 UI 分支下走 UiAnswerResult 契约，产出 explanation/citations/unlock_next；
- unlock_commit 把本轮点亮的节点写入 unlocked_nodes，由 Checkpointer 跨轮累加。

关键设计：
- 三路召回用 Send 并行且各自容错隔离；
- 所有异常都被 safe_node 收敛，图不会因单点失败而中断；
- gate 失败回边（generate <- retry_shrink）是"自愈"通道，最多重试 1 次。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..config import CONFIG
from ..observability import new_trace_id
from ..retrieval.warmup import warmup_index
from ..state import GraphState
from ..ui import (SEED_NODES, STAR_GROUPS, UI_ACTION_CREATE, UI_ACTION_LINK,
                  UI_ACTION_QUERY,
                  UI_ACTION_RESET, UI_ACTION_RESET_ALL, UI_ACTION_STAR,
                  check_unlock_allowed, clean_ui_filters, compute_clickable,
                  known_node_ids, label_for_node, node_brightness_tier, node_is_draft,
                  node_meta)
from .nodes.clarify import clarify_node
from .nodes.create import create_commit
from .nodes.error_node import error_node
from .nodes.fallback import direct_answer, fallback_node, refuse_node
from .nodes.gate import faithfulness_gate
from .nodes.generate import generator
from .nodes.link import link_commit
from .nodes.recall import bm25_recall, metadata_recall, rrf_fusion, vector_recall
from .nodes.rerank import context_compress, rerank, retry_shrink
from .nodes.rewrite import query_rewrite
from .nodes.router import intent_router
from .nodes.unlock import unlock_commit
from .nodes.validate import citation_validate, output_guard


def route_after_rewrite(state: Dict[str, Any]) -> str:
    """纯 UI 动作 / 不可点亮的节点，都不该进检索链路。

    - 重置：不需要改写、路由、召回，直接交给 unlock_commit 清状态；
    - 锁定节点：不在「探索前沿」上时，走完三路召回 + 生成才被告知"没解锁"，
      既浪费一次 LLM，又让"已解锁"的语义变得模糊（内容都生成出来了却不算解锁）。
      这里提前短路，由 unlock_commit 直接产出带 next_hop 的引导卡片。
    """
    ui_action = state.get("ui_action")
    if ui_action in (UI_ACTION_RESET, UI_ACTION_RESET_ALL):
        return "unlock_commit"
    if ui_action == UI_ACTION_LINK:
        return "link_commit"
    if ui_action == UI_ACTION_CREATE:
        return "create_commit"
    if ui_action == UI_ACTION_STAR:
        filters = clean_ui_filters(state.get("ui_filters") or {})
        node_id = str(state.get("star_node_id") or filters.get("node_id") or "")
        ok, _, _ = check_unlock_allowed(
            node_id,
            state.get("unlocked_nodes") or [],
            state.get("session_edges") or [],
            state.get("session_nodes") or {},
            str(filters.get("via") or ""),
        )
        if not ok:
            return "unlock_commit"
    return "intent_router"


def route_after_router(state: Dict[str, Any]):
    """路由出口：越界/闲聊/澄清直接短路；其余进入三路并行召回。"""
    intent = state.get("intent") or "UNCLEAR"
    if intent == "OOS":
        return "refuse"
    if intent == "CHITCHAT":
        return "chitchat"
    if intent == "UNCLEAR":
        return "clarify"
    # Send 并行：三路互不阻塞，单路失败不影响其它路
    return [
        Send("bm25_recall", state),
        Send("vector_recall", state),
        Send("meta_recall", state),
    ]


def route_after_fusion(state: Dict[str, Any]) -> str:
    """相关性闸门：无结果 / 融合分过低 / IDF 相关性过低，都转兜底，不做无依据作答。

    闸门判定放在 rrf_fusion 节点内完成（那里能写 errors），这里只读结果。
    """
    fused = state.get("fused") or []
    if not fused:
        return "fallback"
    stats = state.get("fusion_stats") or {}
    if stats.get("relevant") is False:
        return "fallback"
    return "rerank"


def route_after_gate(state: Dict[str, Any]) -> str:
    if state.get("gate_pass"):
        return "validate"
    if int(state.get("retry_count") or 0) < 1:
        return "retry"
    return "fallback"


def route_after_guard(state: Dict[str, Any]) -> str:
    if not (state.get("final_answer") or "").strip():
        return "error_node"
    return "end"


def build_checkpointer() -> Any:
    """多轮记忆检查点：unlocked_nodes 靠它跨轮累加。

    - 默认 MemorySaver（进程内，够用于单实例 API / Streamlit）；
    - VC_CHECKPOINT=sqlite 时用 SqliteSaver，进程重启后星图进度不丢；
    - 任何导入/初始化失败都回落 MemorySaver，绝不因持久化组件拖垮主链路。
    """
    if os.environ.get("VC_CHECKPOINT", "memory").strip().lower() == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            path = CONFIG.index_dir / "graph_state.sqlite"
            path.parent.mkdir(parents=True, exist_ok=True)
            return SqliteSaver.from_conn_string(str(path))
        except Exception:
            pass
    try:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    except Exception:  # 新版 langgraph 改名为 InMemorySaver 的兼容兜底
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()


def build_query_graph() -> Any:
    g = StateGraph(GraphState)

    g.add_node("query_rewrite", query_rewrite)
    g.add_node("intent_router", intent_router)
    g.add_node("refuse", refuse_node)
    g.add_node("chitchat", direct_answer)
    g.add_node("clarify", clarify_node)

    g.add_node("bm25_recall", bm25_recall)
    g.add_node("vector_recall", vector_recall)
    g.add_node("meta_recall", metadata_recall)
    g.add_node("fusion", rrf_fusion)

    g.add_node("rerank", rerank)
    g.add_node("compress", context_compress)
    g.add_node("generate", generator)
    g.add_node("gate", faithfulness_gate)
    g.add_node("retry_shrink", retry_shrink)
    g.add_node("validate", citation_validate)
    g.add_node("unlock_commit", unlock_commit)
    g.add_node("link_commit", link_commit)
    g.add_node("create_commit", create_commit)
    g.add_node("guard", output_guard)

    g.add_node("fallback", fallback_node)
    g.add_node("error_node", error_node)

    g.add_edge(START, "query_rewrite")
    # ends 必须与 route_after_rewrite 的返回值**逐一对齐**：LangGraph 在这里是
    # ends[返回值] 直接查表，漏一个键要到运行到那条分支才 KeyError ——
    # 静态检查和 import 都不会报错，只能靠端到端跑一遍才暴露。
    g.add_conditional_edges("query_rewrite", route_after_rewrite,
                            {"unlock_commit": "unlock_commit", "intent_router": "intent_router",
                             "link_commit": "link_commit", "create_commit": "create_commit"})

    g.add_conditional_edges(
        "intent_router",
        route_after_router,
        ["refuse", "chitchat", "clarify", "bm25_recall", "vector_recall", "meta_recall"],
    )
    for n in ("bm25_recall", "vector_recall", "meta_recall"):
        g.add_edge(n, "fusion")

    g.add_conditional_edges("fusion", route_after_fusion, {"fallback": "fallback", "rerank": "rerank"})
    g.add_edge("rerank", "compress")
    g.add_edge("compress", "generate")
    g.add_edge("generate", "gate")
    g.add_conditional_edges("gate", route_after_gate, {"validate": "validate", "retry": "retry_shrink", "fallback": "fallback"})
    g.add_edge("retry_shrink", "generate")
    # 引用校验通过后才提交"点亮"：引用不通过的答案不配点亮节点
    g.add_edge("validate", "unlock_commit")
    g.add_edge("unlock_commit", "guard")
    # 连边不点亮节点，因此不接 unlock_commit：它会再做一次邻接校验（而源节点未必在前沿上）
    g.add_edge("link_commit", "guard")
    # 新建节点不接 unlock_commit：它只是加一颗星，没有检索内容可点亮
    g.add_edge("create_commit", "guard")
    g.add_conditional_edges("guard", route_after_guard, {"error_node": "error_node", "end": END})

    g.add_edge("fallback", END)
    g.add_edge("error_node", END)
    g.add_edge("refuse", END)
    g.add_edge("chitchat", END)
    g.add_edge("clarify", END)

    return g.compile(checkpointer=build_checkpointer())


_GRAPH: Optional[Any] = None


def get_query_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_query_graph()
    return _GRAPH


def _rule_edges(session_nodes: Dict[str, Any]) -> List[Dict[str, str]]:
    """规则边（`next` 的无向去重），连同会话节点的一起下发。

    前端画线需要完整拓扑；拓扑必须**同源下发**而不能让前端硬编码一份 next——
    字典一扩容两端必然漂移，用户会看到"有连线却不可点"或反之的鬼影。
    """
    seen: set = set()
    out: List[Dict[str, str]] = []
    for nid in sorted(known_node_ids(session_nodes)):
        for nb in (node_meta(nid, session_nodes).get("next") or []):
            a, b = str(nid), str(nb)
            if a == b:
                continue
            key = (a, b) if a <= b else (b, a)
            if key not in seen:
                seen.add(key)
                out.append({"from": key[0], "to": key[1]})
    return out


def starmap_state(thread_id: str = "default") -> Dict[str, Any]:
    """当前会话的星图全景（只读快照，不跑图、不检索、不生成）。

    存在的理由：用户打开页面时还没有任何 /ask，需要一个初始状态才能渲染首屏；
    否则只能"先问一句才看得到星图"，而星图本该是入口而不是结果。

    亮度档位由服务端下发（前端不自己算），保证与后端的 clickable 判定同源——
    两边各算一遍必然漂移，用户会看到点了没反应的节点。
    """
    unlocked: List[str] = []
    heat: Dict[str, float] = {}
    seen: Dict[str, float] = {}
    session_nodes: Dict[str, Any] = {}
    edges: List[Dict[str, Any]] = []
    try:
        snap = get_query_graph().get_state(
            {"configurable": {"thread_id": thread_id or "default"}})
        v = (getattr(snap, "values", None) or snap or {}) if snap else {}
        unlocked = list(v.get("unlocked_nodes") or [])
        heat = dict(v.get("node_heat") or {})
        seen = dict(v.get("node_last_seen") or {})
        session_nodes = dict(v.get("session_nodes") or {})
        edges = [e for e in (v.get("session_edges") or []) if isinstance(e, dict)]
    except Exception:
        # 读不到快照就按冷启动渲染：星图不该因为一次快照失败而整个打不开
        pass

    return {
        "groups": [
            {"name": name, "color": color,
             "nodes": [{"id": nid, "label": label_for_node(nid, session_nodes)}
                       for nid in members]}
            for name, color, members in STAR_GROUPS
        ],
        "session_nodes": {k: label_for_node(k, session_nodes) for k in session_nodes},
        # 占位节点（内容待补）：前端要把它画成"待补充"且不可点亮，
        # 否则用户点它只会拿到一堆不相干的片段，还以为节点坏了。
        "draft_nodes": [k for k in session_nodes if node_is_draft(k, session_nodes)],
        # 用户自己建的节点（区别于模型提议的）：前端"只看我创建的"过滤要用，
        # 前端拿不到 origin（快照只下发 label），只能由服务端区分。
        "user_nodes": [k for k, v in session_nodes.items()
                       if isinstance(v, dict) and str(v.get("origin") or "") == "user"],
        # 悬停提示要用关键词：前端不存节点字典，只能随快照下发（最多 3 个，与 schema 一致）
        "node_keywords": {nid: list(node_meta(nid, session_nodes).get("keywords") or [])[:3]
                          for nid in known_node_ids(session_nodes)},
        "seed_nodes": list(SEED_NODES),
        "unlocked_nodes": unlocked,
        "clickable": compute_clickable(unlocked, edges, session_nodes),
        "tiers": node_brightness_tier(unlocked, heat, seen, time.time()),
        "session_edges": [
            {"from": e.get("from"), "to": e.get("to"), "origin": e.get("origin", "user")}
            for e in edges
        ],
        "rule_edges": _rule_edges(session_nodes),
    }


def ask(
    query: str = "",
    history: List[Dict[str, str]] = None,
    ui_action: str = UI_ACTION_QUERY,
    ui_filters: Dict[str, Any] = None,
    thread_id: str = "default",
) -> Dict[str, Any]:
    """对外唯一入口：输入问题或 UI 事件，返回完整 state（含 final_answer / ui_payload / trace）。

    thread_id 必传：Checkpointer 按 thread_id 隔离会话，
    否则不同用户的 unlocked_nodes 会串在同一条时间线上（星图进度串台）。
    """
    # 冷启动预热放在节点计时之外：大语料下加载倒排/模型约 10s，压在召回节点里会超时转兜底
    warmup_index()
    state: Dict[str, Any] = {
        "query_raw": query or "",
        "history": history or [],
        "retry_count": 0,
        "trace_id": new_trace_id(),
        "ui_action": ui_action or UI_ACTION_QUERY,
        "ui_filters": clean_ui_filters(ui_filters or {}),
    }
    config = {"configurable": {"thread_id": thread_id or "default"}}
    return get_query_graph().invoke(state, config=config)
