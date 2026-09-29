"""知识星图 v0.5 的回归断言。

每一条都对应一个"如果不这么写就会发生的用户可见故障"，因此测试名写的是故障现象
而不是函数名——改名或重构时，这些现象不应该消失。

不依赖真实索引（纯拓扑与纯计算），因此无语料环境也能跑。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.graph.nodes.link import link_commit  # noqa: E402
from src.vc.graph.nodes.rewrite import _try_link_intent  # noqa: E402
from src.vc.graph.nodes.unlock import unlock_commit  # noqa: E402
from src.vc.state import merge_edges, merge_last_seen  # noqa: E402
from src.vc.ui import (  # noqa: E402
    SEED_NODES,
    UI_ACTION_LINK,
    UI_ACTION_RESET,
    UI_ACTION_RESET_ALL,
    UI_ACTION_STAR,
    VIA_TEXT_LINK,
    apply_pulse,
    check_unlock_allowed,
    compute_clickable,
    dist_G,
    node_brightness_tier,
)

_IDS = ["revenue", "net_profit", "cashflow", "total_assets",
        "business_mix", "gross_margin", "roe", "eps"]


def _heat(v: float = 3.0):
    return {n: v for n in _IDS}


def _seen(now: float):
    """i 越小的节点"越近才看过"。"""
    return {n: now - i * 3600 for i, n in enumerate(_IDS)}


# ============================================================================
# 分档：v0.4 按比例切档的两个致命缺陷
# ============================================================================

def test_tiering_does_not_shift_nodes_user_did_not_touch():
    """故障：用户点亮 A，B 就从"最亮"掉档，而用户对 B 什么都没做。"""
    now = time.time()
    seen = _seen(now)
    t1 = node_brightness_tier(_IDS, _heat(), seen, now)

    seen2 = dict(seen)
    seen2["gross_margin"] = now          # 重新点亮毛利率
    t2 = node_brightness_tier(_IDS, _heat(), seen2, now)

    changed = {n for n in _IDS if t1[n] != t2[n]}
    # 只有"最近 3 / 最久 3"的交界处会动；其余节点必须纹丝不动
    untouched = {"eps", "roe", "total_assets", "net_profit", "revenue"}
    assert changed & untouched == set()
    # 而确实被重新点亮的那个必须变亮
    assert t2["gross_margin"] == "bright"


def test_tiering_refuses_to_invent_a_brightest_node():
    """故障：所有节点都衰退到接近 0，排名却永远存在，硬有几个被标成"最近常看"。"""
    now = time.time()
    tiers = node_brightness_tier(_IDS, _heat(0.01), _seen(now), now)
    assert set(tiers.values()) == {"dim"}


def test_tiering_is_all_mid_when_too_few_nodes():
    """故障：只有 3 个已点亮节点时，bright(3) 与 dim(3) 重叠，分档自相矛盾。"""
    now = time.time()
    few = _IDS[:5]
    tiers = node_brightness_tier(few, _heat(), {n: now for n in few}, now)
    assert set(tiers.values()) == {"mid"}


def test_pulse_lifts_one_tier_then_falls_back():
    """v0.5：脉冲是"临时提档"，不是连续量加成（后者与离散档位没有合并规则）。"""
    now = time.time()
    assert apply_pulse("mid", now - 1.0, now) == "bright"
    assert apply_pulse("mid", now - 99.0, now) == "mid"
    assert apply_pulse("bright", now - 1.0, now) == "bright"   # 封顶
    # 0 是合法时间戳，不能被当成"未提供"
    assert apply_pulse("mid", 0.0, 0.5) == "bright"


# ============================================================================
# 拓扑：会话边不得串联（D19）
# ============================================================================

def test_session_edges_do_not_chain_into_a_tunnel():
    """故障：几条会话边串联把图直径压到 2~3，clickable≈全部，探索前沿自我瓦解。"""
    edges = [{"from": "revenue", "to": "capex"},
             {"from": "capex", "to": "audit_opinion"}]
    assert dist_G("revenue", "capex", edges) == 1                      # 单条边仍然生效
    assert dist_G("revenue", "capex", []) > 1                          # 且确实缩短了距离
    # 但两条串联的路径不参与计算 —— 加了边跟没加一样
    assert dist_G("revenue", "audit_opinion", edges) == dist_G("revenue", "audit_opinion", [])


def test_clickable_is_seed_nodes_at_cold_start():
    """故障：unlocked 为空时按"距离已点亮 ≤1"算，一个都点不了，用户面对全灰的图。"""
    assert compute_clickable([]) == SEED_NODES
    assert "capex" not in compute_clickable([])


def test_clickable_advances_one_step_at_a_time():
    clickable = compute_clickable(["revenue"])
    assert "net_profit" in clickable            # revenue.next 含 net_profit
    assert "audit_opinion" not in clickable     # 远处的定性节点不该一步到位


# ============================================================================
# 点亮校验
# ============================================================================

def test_locked_node_comes_with_a_next_hop():
    """故障：只说"还没解锁"等于把用户丢在原地。"""
    ok, reason, hop = check_unlock_allowed("capex", ["revenue"])
    assert ok is False and reason == "not_frontier"
    assert hop in compute_clickable(["revenue"])


def test_text_link_and_session_node_bypass_the_frontier_check():
    # 正文链接是系统自己给的路径，再判"不相邻"就是把用户刚看到的东西又锁上
    assert check_unlock_allowed("capex", ["revenue"], via=VIA_TEXT_LINK)[0] is True
    # 会话节点是用户提问问出来的，问本身就说明想看；否则会出现"问出来却永远点不亮"
    assert check_unlock_allowed("inventory_ratio", ["revenue"],
                                session_nodes={"inventory_ratio": {"label": "存货周转率"}})[0] is True


def test_unlock_commit_writes_last_seen_along_with_unlock():
    st = {"ui_action": UI_ACTION_STAR, "star_node_id": "revenue",
          "ui_filters": {"node_id": "revenue", "via": "star"},
          "unlocked_nodes": [], "node_heat": {}, "node_last_seen": {},
          "session_nodes": {}, "session_edges": []}
    patch = unlock_commit(st)
    assert patch["unlocked_nodes"] == ["revenue"]
    assert patch["node_last_seen"]["revenue"] > 0


def test_natural_query_turn_does_not_emit_a_locked_card():
    """故障：自然语言回合 star_node_id 为空，被判 unknown_node 而产出卡片，
    覆盖本轮 ui_payload（甚至挂上上一轮的页码）-> 串味卡片。"""
    st = {"ui_action": "natural_query", "star_node_id": "", "ui_filters": {},
          "unlocked_nodes": ["revenue"], "node_heat": {}, "node_last_seen": {},
          "session_nodes": {}, "session_edges": [], "ui_payload": {}}
    patch = unlock_commit(st)
    assert "ui_payload" not in patch      # 不得产出卡片（否则串味）
    assert "unlocked_nodes" not in patch  # 也不得误点亮


def test_reset_is_atomic_and_split_into_two_scopes():
    base = {"unlocked_nodes": ["revenue"], "node_heat": {"revenue": 1.0},
            "node_last_seen": {"revenue": 1.0},
            "session_nodes": {"x": {"label": "X"}},
            "session_edges": [{"from": "revenue", "to": "capex", "origin": "user"}]}
    # 仅重置进度：用户自己造的节点与边必须留下
    p1 = unlock_commit(dict(base, ui_action=UI_ACTION_RESET))
    assert "session_edges" not in p1 and "session_nodes" not in p1
    assert len(p1["ui_payload"]["session_edges"]) == 1
    # 全部清除
    p2 = unlock_commit(dict(base, ui_action=UI_ACTION_RESET_ALL))
    assert p2["session_edges"] and p2["session_nodes"]
    assert p2["ui_payload"]["session_edges"] == []


# ============================================================================
# 连边
# ============================================================================

def _link_base(**kw):
    st = {"ui_action": UI_ACTION_LINK, "unlocked_nodes": [], "node_heat": {},
          "node_last_seen": {}, "session_nodes": {}, "session_edges": []}
    st.update(kw)
    return st


def test_link_requires_confirmation_before_creating_an_edge():
    """故障：模式一要解析 2 个节点却无确认环节，连错了用户无从发现。"""
    p = link_commit(_link_base(star_node_id="revenue",
                               ui_filters={"node_id": "revenue", "link_target": "capex"}))["ui_payload"]
    assert p["type"] == "link_confirm_card"
    assert p["link_target"] == "capex"


def test_link_creates_edge_after_confirmation():
    r = link_commit(_link_base(star_node_id="revenue",
                               ui_filters={"node_id": "revenue", "link_target": "capex",
                                           "link_confirmed": True}))
    assert r["ui_payload"]["type"] == "link_done"
    assert r["session_edges"][0]["origin"] == "user"      # 用户自己说出两端
    assert r["session_edges"][0]["from"] == "revenue"


def test_link_rejects_self_loop_and_duplicates():
    for tgt in ("revenue", "net_profit"):
        p = link_commit(_link_base(star_node_id="revenue",
                                   ui_filters={"node_id": "revenue", "link_target": tgt,
                                               "link_confirmed": True}))["ui_payload"]
        assert p["type"] in ("link_failed", "link_exists")


def test_link_respects_the_edge_budget():
    """故障：上限 20 条足以让 37 节点图的直径塌到 2~3，clickable≈全部。"""
    edges = [{"from": "a%d" % i, "to": "b%d" % i} for i in range(5)]
    p = link_commit(_link_base(star_node_id="revenue", session_edges=edges,
                               ui_filters={"node_id": "revenue", "link_target": "capex",
                                           "link_confirmed": True}))["ui_payload"]
    assert p["type"] == "link_failed" and p["reason"] == "limit_reached"


def test_link_falls_back_to_candidates_when_target_missing():
    p = link_commit(_link_base(star_node_id="revenue",
                               ui_filters={"node_id": "revenue"}))["ui_payload"]
    assert p["type"] == "link_candidates"
    assert 0 < len(p["options"]) <= 3
    assert p["candidates_token"]      # 提交时要用它判断拓扑是否已变


def test_link_rechecks_state_before_creating_edge():
    """故障：候选是很久以前下发的，用户点了却被告知"已连接"。"""
    st = _link_base(star_node_id="revenue",
                    ui_filters={"node_id": "revenue", "link_target": "capex",
                                "link_confirmed": True, "candidates_token": "deadbeef"})
    p = link_commit(st)["ui_payload"]
    assert p["type"] == "link_candidates" and p.get("stale") is True


# ============================================================================
# reducer
# ============================================================================

def test_merge_edges_is_idempotent_and_direction_free():
    a = [{"from": "x", "to": "y"}]
    assert len(merge_edges(a, [{"from": "y", "to": "x"}])) == 1
    assert len(merge_edges(a, [{"from": "z", "to": "w"}])) == 2
    assert len(merge_edges([], [{"from": "e%d" % i, "to": "f%d" % i} for i in range(9)])) == 5


def test_merge_last_seen_keeps_the_latest_timestamp():
    assert merge_last_seen({"a": 1.0}, {"a": 2.0})["a"] == 2.0
    assert merge_last_seen({"a": 5.0}, {"a": 2.0})["a"] == 5.0


# ============================================================================
# 连边意图识别
# ============================================================================

def test_link_intent_requires_an_explicit_verb():
    assert _try_link_intent("把毛利率和净利润连起来") == ("gross_margin", "net_profit")
    # "有什么关系"是提问，不是建边命令——误判会在用户只要解释时偷偷改他的星图
    assert _try_link_intent("毛利率和净利润有什么关系") == ("", "")
    assert _try_link_intent("茅台净利润多少") == ("", "")
