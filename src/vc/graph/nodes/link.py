"""link_commit：用户主动建立**会话边**（把两个节点连起来）。

两种入口，共用一套逻辑：
- 模式一：用户自己说出两端（"毛利率和净利润有什么关系" / 点两个节点再确认）；
- 模式二：用户只给一端，系统出 TOP3 候选供单选。

v0.5 相对初版改了四处，都是修真实会踩到的坑：

1. **模式一也要确认**（原来没有）：模式一要解析 **2 个**节点，比模式二风险更高。
   解析错一个，用户看到"已连接 A ↔ B"而实际连的是别的节点，**他无从发现**；
2. **一端未命中不再整体失败**：改为复用模式二的 TOP3 补全，用户不必把整句话重说一遍；
3. **候选不再排除已直连**：冷启动时最相关的往往都已直连，排除会让候选枯竭甚至为 0，
   右栏一个按钮都没有，用户以为系统卡了。改为**降权 + 标注「已连接」**；
4. **提交时重新判定**：候选基于**下发时刻**的图生成。用户可能隔很久才点，期间拓扑已变。
   直接建边会出现"你给我的选项，点了说已连接"这类荒谬提示。

候选打分当前用**规则**（同组 / next / 图上距离），不调 LLM：
连边是纯拓扑操作，规则可解释、零延迟、可单测；语义打分留到确实出现"规则分不出"的场景再换。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from ...config import CONFIG
from ...decorators import safe_node
from ...ui import (
    STAR_GROUPS,
    UI_ACTION_LINK,
    clean_ui_filters,
    compute_clickable,
    dist_G,
    is_available,
    known_node_ids,
    label_for_node,
    match_node_by_query,
    match_nodes_by_query,
    node_meta,
    unavailable_reason,
)

MAX_CANDIDATES = 3
ORIGIN_USER = "user"    # 模式一：用户自己说出两端
ORIGIN_LLM = "llm"      # 模式二：相关性由系统判断，用户只是在候选里选了一个


def _pair(a: str, b: str) -> Tuple[str, str]:
    """无向边的规范化表示（与 state._edge_key 一致）。"""
    a, b = str(a or ""), str(b or "")
    return (a, b) if a <= b else (b, a)


def _fingerprint(session_edges: List[Dict[str, Any]]) -> str:
    """候选指纹：基于**下发时刻**的会话边集合。

    提交时校验它，能发现"候选下发后拓扑又变了"这种情况，从而重新出候选，
    而不是静默建一条用户本意之外的边。
    """
    keys = sorted(_pair(e.get("from"), e.get("to")) for e in session_edges if isinstance(e, dict))
    raw = json.dumps(keys, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def _is_linked(a: str, b: str, session_edges: List[Dict[str, Any]] = None,
               session_nodes: Dict[str, Any] = None) -> bool:
    """是否已直连（规则边与会话边都算，与 D8 的无向闭包一致）。"""
    if not a or not b:
        return False
    for e in session_edges or []:
        if isinstance(e, dict) and _pair(e.get("from"), e.get("to")) == _pair(a, b):
            return True
    return dist_G(a, b, session_edges, session_nodes) <= 1


def _same_group(a: str, b: str) -> bool:
    for _, _, members in STAR_GROUPS:
        if a in members and b in members:
            return True
    return False


def _score(src: str, cand: str, unlocked: List[str], session_edges: List[Dict[str, Any]],
           session_nodes: Dict[str, Any]) -> float:
    """候选打分：越高越相关。刻意用规则而非 LLM —— 可解释、可单测、零延迟。"""
    s = 0.0
    if cand in (node_meta(src, session_nodes).get("next") or []):
        s += 3.0                                   # 字典里人工标注的推荐后继，最可信
    if _same_group(src, cand):
        s += 2.0                                   # 同分组语义更近
    d = dist_G(src, cand, session_edges, session_nodes)
    s += max(0.0, 3.0 - d)                         # 图上越近越相关
    if cand in set(unlocked):
        s -= 1.0                                   # 已点亮：连边的边际价值低（内容已经能看）
    if _is_linked(src, cand, session_edges, session_nodes):
        s -= 5.0                                   # 已直连：降权但**不排除**（见模块 docstring）
    return s


def _candidates(src: str, unlocked: List[str], session_edges: List[Dict[str, Any]],
                session_nodes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """TOP3 候选。排除自身与 unavailable；已直连只降权并标注。不足 3 个就按实际数量给，不硬凑。"""
    ids = known_node_ids(session_nodes)
    scored: List[Tuple[float, str]] = []
    for nid in sorted(ids):
        if nid == src or not is_available(nid):
            continue
        scored.append((_score(src, nid, unlocked, session_edges, session_nodes), nid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: List[Dict[str, Any]] = []
    for sc, nid in scored[:MAX_CANDIDATES]:
        out.append({
            "node_id": nid,
            "label": label_for_node(nid, session_nodes),
            "linked": _is_linked(src, nid, session_edges, session_nodes),
            "unlocked": nid in set(unlocked),
            "score": round(sc, 3),
        })
    return out


def _resolve(target: str, ids: set, session_nodes: Dict[str, Any]) -> Optional[str]:
    """解析目标端：先当 node_id，再用自然语言匹配。解析不出来返回 None。"""
    t = str(target or "").strip().lower()
    if not t:
        return None
    if t in ids:
        return t
    return match_node_by_query(t, session_nodes)


def _view(unlocked: List[str], state: Dict[str, Any], session_edges: List[Dict[str, Any]],
          session_nodes: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "unlocked_nodes": list(unlocked),
        "clickable": compute_clickable(unlocked, session_edges, session_nodes),
        "session_edges": [
            {"from": e.get("from"), "to": e.get("to"), "origin": e.get("origin", ORIGIN_USER)}
            for e in session_edges if isinstance(e, dict)
        ],
    }


def _done_patch(text: str, payload_extra: Dict[str, Any], unlocked: List[str],
                state: Dict[str, Any], session_edges: List[Dict[str, Any]],
                session_nodes: Dict[str, Any]) -> Dict[str, Any]:
    payload = {"citations": [], "unlock_next": [], "refused": False, "bypass_guard": True}
    payload.update(payload_extra)
    payload.update(_view(unlocked, state, session_edges, session_nodes))
    return {"final_answer": text, "ui_payload": payload}


@safe_node("link_commit", timeout=0.5)
def link_commit(state: Dict[str, Any]) -> Dict[str, Any]:
    session_nodes = state.get("session_nodes") or {}
    session_edges = [e for e in (state.get("session_edges") or []) if isinstance(e, dict)]
    unlocked = list(state.get("unlocked_nodes") or [])
    filters = clean_ui_filters(state.get("ui_filters") or {})
    ids = known_node_ids(session_nodes)

    src = str(state.get("star_node_id") or filters.get("node_id") or "").strip().lower()
    if src not in ids:
        return _done_patch(
            "先选一个已存在的指标作为连线起点。",
            {"type": "link_failed", "reason": "unknown_source"},
            unlocked, state, session_edges, session_nodes,
        )
    if not is_available(src):
        return _done_patch(
            "「%s」暂不可用，不能作为连线起点：%s" % (label_for_node(src, session_nodes),
                                            unavailable_reason(src)),
            {"type": "link_failed", "reason": "unavailable_source", "node_id": src},
            unlocked, state, session_edges, session_nodes,
        )

    raw_target = str(filters.get("link_target") or "")
    tgt = _resolve(raw_target, ids, session_nodes)

    # ---- 模式二 / 部分命中：出候选，不建边 ----
    if tgt is None:
        # 用户一句口述里可能同时给了两端（"毛利率和净利润"），这里再试一次双端解析
        if raw_target:
            both = match_nodes_by_query(raw_target, session_nodes, limit=2)
            if len(both) >= 2:
                src, tgt = both[0], both[1]
        if tgt is None:
            opts = _candidates(src, unlocked, session_edges, session_nodes)
            la, lb = label_for_node(src, session_nodes), ""
            text = ("「%s」可以和这些指标建立关联：" % la) if opts else \
                   ("暂时找不到适合与「%s」关联的指标。" % la)
            return _done_patch(
                text,
                {"type": "link_candidates", "node_id": src, "label": la, "options": opts,
                 "candidates_token": _fingerprint(session_edges)},
                unlocked, state, session_edges, session_nodes,
            )

    if tgt == src:
        return _done_patch("不能把一个指标连到它自己。",
                           {"type": "link_failed", "reason": "self_loop", "node_id": src},
                           unlocked, state, session_edges, session_nodes)

    la, lb = label_for_node(src, session_nodes), label_for_node(tgt, session_nodes)

    # ---- 目标可用性：禁止连到 unavailable（幽灵边，见模块 docstring）----
    if not is_available(tgt):
        return _done_patch(
            "「%s」暂不可用，先连别的指标：%s" % (lb, unavailable_reason(tgt)),
            {"type": "link_failed", "reason": "unavailable_target", "node_id": tgt},
            unlocked, state, session_edges, session_nodes,
        )

    # ---- 模式一：未确认则出确认卡片 ----
    if not filters.get("link_confirmed"):
        return _done_patch(
            "即将连接「%s」↔「%s」，确认吗？" % (la, lb),
            {"type": "link_confirm_card", "node_id": src, "label": la,
             "link_target": tgt, "link_label": lb,
             "candidates_token": _fingerprint(session_edges)},
            unlocked, state, session_edges, session_nodes,
        )

    # ---- 提交时重新判定：候选可能是很久以前下发的，拓扑已变 ----
    token = str(filters.get("candidates_token") or "")
    if token and token != _fingerprint(session_edges):
        opts = _candidates(src, unlocked, session_edges, session_nodes)
        return _done_patch(
            "星图已经变了，重新选一个要关联的指标：",
            {"type": "link_candidates", "node_id": src, "label": la, "options": opts,
             "candidates_token": _fingerprint(session_edges), "stale": True},
            unlocked, state, session_edges, session_nodes,
        )
    if tgt in set(unlocked):
        # 用户点了候选却发现它已点亮 —— 直接打开它比建一条无意义的边更贴合意图
        return _done_patch(
            "「%s」已经点亮了，直接看它的内容就好。" % lb,
            {"type": "link_open", "node_id": tgt, "label": lb},
            unlocked, state, session_edges, session_nodes,
        )
    if _is_linked(src, tgt, session_edges, session_nodes):
        return _done_patch(
            "「%s」和「%s」已经关联了。" % (la, lb),
            {"type": "link_exists", "node_id": src, "link_target": tgt},
            unlocked, state, session_edges, session_nodes,
        )
    if len(session_edges) >= CONFIG.max_session_edges:
        return _done_patch(
            "本会话最多创建 %d 条关联，先删掉一些再连。" % CONFIG.max_session_edges,
            {"type": "link_failed", "reason": "limit_reached",
             "limit": CONFIG.max_session_edges},
            unlocked, state, session_edges, session_nodes,
        )

    # ---- 建边 ----
    origin = ORIGIN_USER if raw_target else ORIGIN_LLM
    edge = {"from": src, "to": tgt, "origin": origin, "created_at": time.time()}
    payload_extra = {
        "type": "link_done", "node_id": src, "label": la,
        "link_target": tgt, "link_label": lb, "origin": origin,
    }
    payload_extra.update(_view(unlocked, state, session_edges + [edge], session_nodes))
    return {
        "session_edges": [edge],
        "final_answer": "已连接「%s」↔「%s」。" % (la, lb),
        "ui_payload": dict({"citations": [], "unlock_next": [], "refused": False,
                            "bypass_guard": True}, **payload_extra),
    }
