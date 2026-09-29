"""unlock_commit：把本轮「已点亮」写进 unlocked_nodes。

语义边界（容易写错的一处）：
- 只写**本次实际点亮**的节点；
- unlock_next 是推荐候选，留在 ui_payload 里由前端决定何时点亮，不冒充已点亮
  （否则星图会出现"没读过却已亮"的节点，用户点进去发现没内容）。

v0.5 新增三件事：
1. **邻接校验**：不在「探索前沿」上的节点**不点亮**，直接回一张锁定引导卡片。
   卡片必须带 next_hop（"先看 X"）—— 只说"未解锁"等于把用户丢在原地；
2. **last_seen**：点亮时写时间戳。分档只按它排序（见 ui.node_brightness_tier），
   heat 是累加量，本身表达不了"最近看过什么"；
3. **reset 拆两档 + 原子清除**：默认只清进度，用户自己造的会话节点与边不被
   一个按钮抹掉（他可能花了十几轮才建出来）。

累加与跨轮持久化由两件事共同保证：
1. state.unlocked_nodes 使用 merge_unique reducer（去重累加）；
2. 图在 compile 时注入 Checkpointer，同一 thread_id 下跨 invoke 保留。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from ...config import CONFIG
from ...decorators import safe_node
from ...state import RESET_DICT, RESET_LIST
from ...ui import (
    STAR_GROUPS,
    UI_ACTION_RESET,
    UI_ACTION_RESET_ALL,
    check_unlock_allowed,
    clean_ui_filters,
    compute_clickable,
    known_node_ids,
    label_for_node,
    node_brightness_tier,
    unavailable_reason,
)

# 热度权重：直接点击算一次完整关联，被推荐算半次。
# 与 config 保持单一数据源，避免"这里改了、渲染那边没改"。
HEAT_DIRECT = CONFIG.heat_direct
HEAT_INDIRECT = CONFIG.heat_indirect


def _alternatives(node_id: str, unlocked: List[str], session_edges: List[Dict[str, Any]],
                  session_nodes: Dict[str, Any], limit: int = 2) -> List[str]:
    """死路出口：给出同分组内、当前可点亮的替代指标。

    硬编码一句"当前版本未接入"就结束了，用户点三次得到三句一样的话、没有任何
    下一步 —— 死路必须变成活路。这里优先给同组（语义最近），不足再补任意可点亮节点。
    """
    clickable = set(compute_clickable(unlocked, session_edges, session_nodes))
    group: List[str] = []
    for _, _, members in STAR_GROUPS:
        if node_id in members:
            group = list(members)
            break
    out = [n for n in group if n != node_id and n in clickable]
    if len(out) < limit:
        out += [n for n in sorted(clickable) if n != node_id and n not in out]
    return out[:limit]


def _starmap_view(unlocked: List[str], state: Dict[str, Any],
                  session_edges: List[Dict[str, Any]], session_nodes: Dict[str, Any],
                  now: float, heat_delta: Dict[str, float] = None,
                  seen_delta: Dict[str, float] = None) -> Dict[str, Any]:
    """本轮之后的星图全景，供前端增量渲染（避免前端自己再算一遍、与后端漂移）。"""
    heat = dict(state.get("node_heat") or {})
    for k, v in (heat_delta or {}).items():
        heat[k] = float(heat.get(k, 0.0)) + float(v)
    seen = dict(state.get("node_last_seen") or {})
    seen.update(seen_delta or {})
    return {
        "unlocked_nodes": list(unlocked),
        "clickable": compute_clickable(unlocked, session_edges, session_nodes),
        "tiers": node_brightness_tier(unlocked, heat, seen, now),
        "session_edges": [
            {"from": e.get("from"), "to": e.get("to"), "origin": e.get("origin", "user")}
            for e in session_edges if isinstance(e, dict)
        ],
    }


def _locked_patch(node_id: str, reason: str, hop: str, unlocked: List[str],
                  session_edges: List[Dict[str, Any]],
                  session_nodes: Dict[str, Any],
                  state: Dict[str, Any]) -> Dict[str, Any]:
    """不可点亮：不写状态，回一张带出口的引导卡片。

    state 必须传进来：已点亮节点的亮度要按**真实** heat/last_seen 渲染，
    传空会让它们全变成 dim —— 用户刚看过的节点凭空变暗，像是系统出了故障。
    """
    label = label_for_node(node_id, session_nodes) or node_id or "该指标"
    alts = _alternatives(node_id, unlocked, session_edges, session_nodes)
    if reason == "unavailable":
        why = unavailable_reason(node_id) or "当前版本暂未接入该数据源"
        text = "「%s」暂不可用：%s。" % (label, why)
    elif reason == "unknown_node":
        text = "「%s」还不在星图里。" % (node_id or "该指标")
    elif reason == "draft":
        # 空节点没有关键词，点亮它只会召回不相干的片段。这里必须说清"差什么、
        # 补了就能亮"，否则用户以为系统坏了，而不是自己还没填内容。
        text = ("「%s」是你新建的占位节点，还没有内容，暂时点不亮。"
                "补充关键词（报告里会出现的表述）后即可点亮。" % label)
    else:
        hop_label = label_for_node(hop, session_nodes) if hop else ""
        if not hop_label and alts:
            # 冷启动时没有已点亮节点做锚点，next_hop 算不出来。此时退到备选的第一个，
            # 否则文案只剩"还没解锁"——说了等于没说，用户不知道下一步该点哪。
            hop_label = label_for_node(alts[0], session_nodes)
        text = ("「%s」还没解锁，先看看「%s」。" % (label, hop_label)
                if hop_label else "「%s」还没解锁。" % label)
    payload = {
        "node_id": node_id,
        "label": label,
        "locked": True,
        "lock_reason": reason,
        "next_hop": hop or "",
        "alternatives": alts,
        "explanation": text,
        "citations": [],
        "unlock_next": [],
        "refused": False,
        "bypass_guard": True,
    }
    if reason == "draft":
        # 前端据此给"补充内容"入口：空节点唯一的活路就是补内容，
        # 只说"点不亮"而没给补全入口，用户只能干瞪眼。
        payload["fillable"] = True
    payload.update(_starmap_view(unlocked, state, session_edges, session_nodes, time.time()))
    return {"final_answer": text, "ui_payload": payload}


def _reset_patch(ui_action: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """重置：原子清除，绝不出现"清了进度却留着边"这种半清状态。

    半清状态会让星图自相矛盾：unlocked 空了但 session_edges 还在，
    而边的语义是"用户连的"，用户却看不到自己连过什么。
    """
    all_scope = ui_action == UI_ACTION_RESET_ALL
    patch: Dict[str, Any] = {
        "unlocked_nodes": RESET_LIST,
        "node_heat": RESET_DICT,
        "node_last_seen": RESET_DICT,
    }
    if all_scope:
        patch["session_nodes"] = RESET_DICT
        patch["session_edges"] = RESET_LIST
        text = "已清除本会话的全部星图改动（进度、新节点、连线）。"
    else:
        text = "已重置点亮进度，你创建的新节点与连线都保留了。"
    # 回传时按"清除后"的真实内容算：仅重置进度时边与新节点仍在，
    # 前端要据此重画，否则用户看到连线凭空消失、却不知道为什么。
    kept_edges = [] if all_scope else [e for e in (state.get("session_edges") or [])
                                       if isinstance(e, dict)]
    kept_nodes = {} if all_scope else (state.get("session_nodes") or {})
    payload = {
        "node_id": "", "label": "", "explanation": text,
        "citations": [], "unlock_next": [], "refused": False, "reset": True,
        "bypass_guard": True,
    }
    payload.update(_starmap_view([], state, kept_edges, kept_nodes, time.time()))
    patch["final_answer"] = text
    patch["ui_payload"] = payload
    return patch


@safe_node("unlock_commit", timeout=0.5)
def unlock_commit(state: Dict[str, Any]) -> Dict[str, Any]:
    ui_action = state.get("ui_action") or ""
    if ui_action in (UI_ACTION_RESET, UI_ACTION_RESET_ALL):
        return _reset_patch(ui_action, state)

    session_nodes = state.get("session_nodes") or {}
    session_edges = [e for e in (state.get("session_edges") or []) if isinstance(e, dict)]
    unlocked = list(state.get("unlocked_nodes") or [])
    filters = clean_ui_filters(state.get("ui_filters") or {})
    node_id = str(state.get("star_node_id") or filters.get("node_id") or "")
    via = str(filters.get("via") or "")

    now = time.time()
    if not node_id:
        # 自然语言回合：没有要点亮的节点。此时绝不能产卡片 ——
        # 那会覆盖本轮的自然语言 ui_payload（甚至把上轮卡片的页码挂上去），
        # 前端渲染出"上轮节点 + 本轮引用"的串味卡片。
        payload = dict(state.get("ui_payload") or {})
        if payload:
            payload.update(_starmap_view(unlocked, state, session_edges, session_nodes, now))
            return {"ui_payload": payload}
        return {}

    # 邻接校验在前：不在前沿上就没必要召回与生成，直接回引导卡片。
    # 省一次三路召回 + 一次生成，也避免"用户看到内容了但节点没亮"的不一致。
    ok, reason, hop = check_unlock_allowed(node_id, unlocked, session_edges, session_nodes, via)
    if not ok:
        return _locked_patch(node_id, reason, hop or "", unlocked, session_edges,
                             session_nodes, state)

    allowed = known_node_ids(session_nodes)
    patch: Dict[str, Any] = {}
    seen_delta: Dict[str, float] = {}
    if node_id and node_id in allowed:
        patch["unlocked_nodes"] = [node_id]
        seen_delta[node_id] = now

    payload = dict(state.get("ui_payload") or {})
    merged = list(unlocked)
    for n in patch.get("unlocked_nodes") or []:
        if n not in merged:
            merged.append(n)

    # 热度：直接点击记满分，本轮推荐的候选节点各记半分（间接关联）。
    # 只统计白名单内节点——模型造词未经验证时不该给热度，否则会造出幽灵感。
    heat: Dict[str, float] = {}
    if node_id and node_id in allowed:
        heat[node_id] = HEAT_DIRECT
    for n in (payload.get("unlock_next") or []):
        n = str(n)
        if n in allowed and n != node_id:
            heat[n] = heat.get(n, 0.0) + HEAT_INDIRECT
    if heat:
        patch["node_heat"] = heat
    if seen_delta:
        patch["node_last_seen"] = seen_delta

    payload.update(_starmap_view(merged, state, session_edges, session_nodes, now,
                                 heat_delta=heat, seen_delta=seen_delta))
    patch["ui_payload"] = payload
    return patch
