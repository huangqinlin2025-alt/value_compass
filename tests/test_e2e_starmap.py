"""端到端路试：整图 invoke（不是节点级单测），验证"边"和 Checkpointer 跨轮行为。

与 test_ui_graph.py 的分工：
- 那边验证零件（节点函数的输入输出）；
- 这边验证整车（START -> ... -> END 的真实流转、Send 并行的写入时序、
  同一 thread_id 下 unlocked_nodes 是否真的从 checkpoint 恢复并累加）。

依赖真实索引（index/manifest.json）。索引缺失时整文件 skip，不阻塞无语料环境。

耗时：warmup 后每轮约 0.5~1s，全量约 8s（首次含索引加载会到 ~10s）。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.config import CONFIG  # noqa: E402
from src.vc.graph.query_graph import ask  # noqa: E402
from src.vc.ui import UI_ACTION_RESET, UI_ACTION_STAR  # noqa: E402

pytestmark = pytest.mark.skipif(
    not CONFIG.manifest_path.exists(),
    reason="需要已构建的检索索引（index/manifest.json），无语料环境跳过",
)

# 语料里真实存在的标的（库内 stock_code 为 A 股六位码，例如 600519）
COMPANY = "600519"

# 整图主链路必经节点（Send 三路并行，顺序不固定，故用集合断言）
MAIN_PATH = {
    "query_rewrite", "intent_router",
    "bm25_recall", "vector_recall", "metadata_recall", "rrf_fusion",
    "rerank", "context_compress", "generator",
    "faithfulness_gate", "citation_validate", "unlock_commit", "output_guard",
}


def _thread() -> str:
    """每个用例用独立 thread_id：Checkpointer 按 thread 隔离，避免用例间互相污染。"""
    return "e2e-%s" % uuid.uuid4().hex[:8]


def _nodes(result: dict) -> set:
    return {t.get("node") for t in (result.get("trace") or [])}


def test_click_star_runs_full_graph_and_unlocks():
    tid = _thread()
    # 冷启动只有种子点可点（渐进式披露的起点）：营业收入是种子点，毛利率不是。
    # 直接点毛利率会被判"不在探索前沿"，因此先点亮营业收入，再点亮与它相邻的毛利率。
    ask(ui_action=UI_ACTION_STAR,
        ui_filters={"node_id": "revenue", "company": COMPANY}, thread_id=tid)
    r = ask(ui_action=UI_ACTION_STAR,
            ui_filters={"node_id": "gross_margin", "company": COMPANY}, thread_id=tid)

    assert MAIN_PATH <= _nodes(r)                       # 整条主链路真的走完了（含 Send 三路）
    assert r["intent"] == "METRIC"                      # 节点 id 即意图，未经过 LLM
    assert r["route_cfg"]["source"] == "ui_star"
    assert r["unlocked_nodes"] == ["revenue", "gross_margin"]   # 点亮已累加

    payload = r["ui_payload"]
    assert payload["node_id"] == "gross_margin"
    assert payload["label"] == "毛利率"
    assert len(payload["explanation"]) <= 350           # 卡片硬约束（两期对比要放得下）           # 卡片硬约束
    assert isinstance(payload["citations"], list)
    assert payload["unlock_next"]                       # 渐进式披露必须有后继
    assert payload["disclaimer"]                        # 免责声明独立下发
    assert r["final_answer"]


def test_unlocked_nodes_accumulate_across_invokes():
    """核心假设：同一 thread_id 下 unlocked_nodes 从 checkpoint 恢复并累加（不靠调用方回传）。"""
    tid = _thread()
    # 第一跳必须是种子点（冷启动），之后才能沿邻接继续点亮
    r1 = ask(ui_action=UI_ACTION_STAR,
             ui_filters={"node_id": "revenue", "company": COMPANY}, thread_id=tid)
    r2 = ask(ui_action=UI_ACTION_STAR,
             ui_filters={"node_id": "net_profit", "company": COMPANY}, thread_id=tid)

    assert r1["unlocked_nodes"] == ["revenue"]
    assert r2["unlocked_nodes"] == ["revenue", "net_profit"]
    # 已点亮的节点不应再被推荐（渐进式披露只往前走）
    assert "revenue" not in (r2["ui_payload"].get("unlock_next") or [])


def test_natural_query_does_not_bleed_previous_ui_payload():
    """回归：ui_payload 是覆盖写字段，自然语言轮次不清空就会恢复出上一轮的卡片，
    再被 output_guard 同步上本轮页码 —— 前端渲染成"上轮节点 + 本轮引用"的串味卡片。"""
    tid = _thread()
    ask(ui_action=UI_ACTION_STAR,
        ui_filters={"node_id": "net_profit", "company": COMPANY}, thread_id=tid)
    r = ask("贵州茅台的营业收入是多少？", thread_id=tid)

    assert r["unlocked_nodes"] == ["net_profit"]        # 进度保留，但本轮不得新点亮
    assert r["ui_payload"] in ({}, None)                # 卡片载荷不得残留（否则串味）
    assert r["star_node_id"] == ""                      # 不清会让本轮去点亮上轮的节点
    assert r["filters_strict"] is False                 # 严格过滤不得泄漏到自然语言回合
    assert r["route_cfg"]["source"] != "ui_star"        # 自然语言走规则路由
    assert r["final_answer"]


def test_reset_clears_unlocked_nodes():
    tid = _thread()
    ask(ui_action=UI_ACTION_STAR,
        ui_filters={"node_id": "gross_margin", "company": COMPANY}, thread_id=tid)
    r = ask(ui_action=UI_ACTION_RESET, thread_id=tid)

    assert r["unlocked_nodes"] == []
    assert r["ui_payload"].get("reset") is True
    assert r["ui_payload"].get("citations") == []      # 不得挂上上一轮残留的页码
    assert _nodes(r) == {"query_rewrite", "unlock_commit", "output_guard"}  # 重置不进检索


def test_unknown_company_falls_back_without_unlocking():
    """库外标的（港股 0700）+ filters_strict：召回为空也不放宽，转兜底且不点亮。"""
    r = ask(ui_action=UI_ACTION_STAR,
            ui_filters={"node_id": "revenue", "company": "0700"},
            thread_id=_thread())

    # 走兜底则不经过 unlock_commit：没读到内容就不该点亮
    assert "unlock_commit" not in _nodes(r) or r["unlocked_nodes"] == []
    assert r["unlocked_nodes"] == []
    # 但卡片不能空：UI 永不空屏
    assert r["ui_payload"]["node_id"] == "revenue"
    assert r["ui_payload"]["refused"] is True
    assert r["fallback_reason"]
