"""富交互（知识星图）链路单测：事件清洗、路由短路、结构化输出、多轮记忆。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.graph.nodes.fallback import fallback_node  # noqa: E402
from src.vc.graph.nodes.generate import (  # noqa: E402
    _cites_by_period,
    _verify_new_nodes,
    generator,
)
from src.vc.graph.nodes.rewrite import query_rewrite  # noqa: E402
from src.vc.graph.nodes.router import intent_router  # noqa: E402
from src.vc.graph.nodes.unlock import unlock_commit  # noqa: E402
from src.vc.graph.nodes.validate import output_guard  # noqa: E402
from src.vc.graph.query_graph import build_query_graph, route_after_rewrite  # noqa: E402
from src.vc.schema import UiAnswerResult  # noqa: E402
from src.vc.retrieval.filters import chunk_matches  # noqa: E402
from src.vc.retrieval.probe import probe_node  # noqa: E402
from src.vc.state import (  # noqa: E402
    RESET_DICT,
    RESET_LIST,
    add_list,
    merge_heat,
    merge_nodes,
    merge_unique,
)
from src.vc.ui import (  # noqa: E402
    UI_ACTION_FOLLOWUP,
    UI_ACTION_QUERY,
    UI_ACTION_RESET,
    UI_ACTION_STAR,
    clean_ui_filters,
    STAR_GROUPS,
    STAR_NODES,
    clean_unlock_next,
    known_node_ids,
    node_meta,
    star_query,
    to_filters,
)

STAR_STATE = {"ui_action": UI_ACTION_STAR, "ui_filters": {"node_id": "gross_margin", "company": "0700"}}


def test_clean_ui_filters_drops_unknown_keys_and_normalizes_code():
    out = clean_ui_filters({"node_id": "Gross_Margin", "company": "0700", "evil": "x", "stock_code": None})
    assert out["node_id"] == "gross_margin"      # 归一为小写
    assert out["stock_code"] == "0700"           # company 里的代码被抽成过滤维度
    assert "evil" not in out                     # 非白名单字段一律丢弃
    assert "stock_code" in to_filters(out)       # 可进检索过滤
    assert "node_id" not in to_filters(out)      # node_id 不参与 chunk 过滤


def test_star_query_includes_company_and_period():
    q = star_query("gross_margin", {"company": "腾讯", "report_period": "2026H1"})
    assert "腾讯" in q and "毛利率" in q and "2026年半年度" in q


def test_ui_answer_schema_truncates_and_normalizes():
    r = UiAnswerResult.model_validate({
        "explanation": "长" * 500,
        "citations": [12, {"page": 9}, "P7", "P7"],
        "unlock_next": ["revenue", "revenue", "net_profit", "cashflow"],
    })
    assert len(r.explanation) == 350             # 超长硬截断到 350（两期对比要放得下），不整条丢弃
    assert r.citations == ["P12", "P9", "P7"]    # 三种脏写法归一 + 去重
    assert len(r.unlock_next) <= 2               # LLM 给多了也只留 2 个


def test_chunk_matches_accepts_any_of_report_periods():
    c24 = {"stock_code": "600519", "report_period": "2024A"}
    c25 = {"stock_code": "600519", "report_period": "2025A"}
    c23 = {"stock_code": "600519", "report_period": "2023A"}
    f = {"stock_code": "600519", "report_periods": ["2024A", "2025A"]}
    assert chunk_matches(c24, f) and chunk_matches(c25, f)   # 两期都要能进
    assert not chunk_matches(c23, f)                        # 期外仍被挡住
    assert not chunk_matches({"stock_code": "000651", "report_period": "2025A"}, f)


def test_cites_by_period_splits_pages_per_period():
    # 报表页码结构逐年固定：「营业收入」两期都在 P54，靠页码反查期间必然判不明，
    # 所以按期间分组，每组配自己的期间后页码才唯一。
    ctx = [{"page": 54, "report_period": "2024A"},
           {"page": 54, "report_period": "2025A"},
           {"page": 60, "report_period": "2025A"}]
    out = _cites_by_period(["P54", "P60"], ctx, ["2024A", "2025A"])
    assert out["2024A"] == ["P54"]
    assert out["2025A"] == ["P54", "P60"]


def test_probe_node_accepts_real_terms_rejects_fabricated():
    # 造词被拦住的意义：不加这道闸门，模型会造出报告里没有的科目，
    # 用户点进去是空卡片，而空卡片在链路里表现为"转兜底"，trace 上看不出是节点造错了。
    ok_real, hit_real, _ = probe_node(["毛利率"], filters={"stock_code": "600519"})
    assert ok_real and hit_real >= 0.5
    ok_fake, hit_fake, _ = probe_node(["量子计算"], filters={"stock_code": "600519"})
    assert not ok_fake and hit_fake == 0.0


def test_verify_new_nodes_keeps_only_recallable():
    st = {"filters": {"stock_code": "600519"}}
    out = _verify_new_nodes(
        [{"id": "存货周转", "label": "存货周转率", "keywords": ["存货"]},
         {"id": "pe_ratio", "label": "市盈率", "keywords": ["市盈率"]},
         {"id": "broken", "label": "缺关键词"}],
        st,
    )
    assert "存货周转" in out                    # 报告里真有这个科目
    assert "pe_ratio" not in out               # 定期报告不披露 -> 不能进星图
    assert "broken" not in out                 # 没给关键词，无法验证 -> 不放行


def test_session_node_is_first_class_citizen():
    sn = {"inventory_ratio": {"label": "存货周转率", "keywords": ["存货"], "intent": "METRIC"}}
    assert node_meta("inventory_ratio", sn)["label"] == "存货周转率"
    assert "存货" in star_query("inventory_ratio", {"company": "贵州茅台"}, sn)
    assert "inventory_ratio" in known_node_ids(sn)
    # 通过验证才能在推荐位出现；没验证过的 id 一律不放行（否则是点不动的幽灵节点）
    assert clean_unlock_next(["inventory_ratio"], session_nodes=sn) == ["inventory_ratio"]
    assert clean_unlock_next(["inventory_ratio"]) == []


def test_unlock_commit_accepts_verified_session_node():
    st = {"star_node_id": "inventory_ratio",
          "session_nodes": {"inventory_ratio": {"label": "存货周转率"}}}
    patch = unlock_commit(st)
    assert patch.get("unlocked_nodes") == ["inventory_ratio"]


def test_merge_nodes_accumulates_overrides_and_resets():
    a = {"x": {"label": "X"}}
    assert merge_nodes(a, {"y": {"label": "Y"}}) == {"x": {"label": "X"}, "y": {"label": "Y"}}
    # 同 id 再次出现取最新（模型可能给出更准的 keywords），不是 dict 相加
    assert merge_nodes(a, {"x": {"label": "X2"}})["x"]["label"] == "X2"
    assert merge_nodes(a, RESET_DICT) == {}


def test_star_groups_cover_every_node_exactly_once():
    # 新增字典节点忘了加分组 -> 前端漏渲染，且不会报错，只能靠这条断言兜住
    grouped = [nid for _name, _tone, ids in STAR_GROUPS for nid in ids]
    assert len(grouped) == len(set(grouped)), "同一节点被分进多个组：%s" % (
        [n for n in grouped if grouped.count(n) > 1])
    assert sorted(grouped) == sorted(STAR_NODES), "分组与字典不一致：%s" % (
        sorted(set(grouped) ^ set(STAR_NODES)))


def test_merge_heat_accumulates_and_resets():
    h = merge_heat({"revenue": 1.0}, {"revenue": 0.5, "roe": 0.5})
    assert h == {"revenue": 1.5, "roe": 0.5}     # 直接点击 1.0 + 被推荐 0.5 累加
    assert merge_heat({"revenue": 1.5}, RESET_DICT) == {}   # reset_starmap 仍能清空


def test_clean_unlock_next_rejects_ghost_nodes():
    out = clean_unlock_next(["revenue", "ghost_node", "revenue", "cashflow"], "net_profit", ["cashflow"])
    assert out == ["revenue"]                    # 幽灵节点、当前节点、已点亮都被剔除


def test_rewrite_bypasses_query_raw_for_ui_events():
    out = query_rewrite(dict(STAR_STATE))
    assert out["star_node_id"] == "gross_margin"
    assert out["query_rewritten"]                # 空的 query_raw 也能产出检索词
    assert out["filters"] == {"stock_code": "0700"}
    # 新回合哨兵由 reducer 过滤掉，不会污染 errors
    assert add_list([{"code": "OLD"}], out["errors"]) == []


def test_rewrite_still_rejects_empty_natural_query():
    out = query_rewrite({"ui_action": UI_ACTION_QUERY, "query_raw": ""})
    # 空问题分支带 RESET_LIST 哨兵：经 reducer 后只剩本轮错误，上轮残留被清掉
    errs = add_list([{"code": "OLD"}], out["errors"])
    assert len(errs) == 1 and errs[0]["message"] == "空问题"
    # trace 同理清空；safe_node 会在返回后追加本节点自身的 trace，故只剩 query_rewrite
    assert [t.get("node") for t in add_list(["old_node"], out["trace"])] == ["query_rewrite"]


def test_rewrite_clears_stale_ui_state_for_natural_query():
    """自然语言轮次必须清掉上一轮点击留下的 UI 残留。

    ui_payload / star_node_id / filters_strict 都是覆盖写字段，Checkpointer 会原样恢复：
    不清会让卡片串味、让自然语言提问去点亮上轮节点、让严格过滤泄漏到自然语言回合。
    """
    out = query_rewrite({"ui_action": UI_ACTION_QUERY, "query_raw": "贵州茅台的营业收入是多少？",
                         "star_node_id": "net_profit", "filters_strict": True})
    assert out["ui_payload"] == {}
    assert out["star_node_id"] == ""
    assert out["filters_strict"] is False

    # 空问题分支同样要清，否则错误回合也会带上残留
    out2 = query_rewrite({"ui_action": UI_ACTION_QUERY, "query_raw": "", "star_node_id": "net_profit"})
    assert out2["ui_payload"] == {} and out2["star_node_id"] == ""

    # 星图点击分支不得被清掉（那是本轮真正要产出的载荷）
    star = query_rewrite(dict(STAR_STATE))
    assert "ui_payload" not in star and star["star_node_id"] == "gross_margin"


def test_router_short_circuits_without_llm(monkeypatch):
    """点击星图时不得触碰 LLM：把 provider 打成必失败，短路分支仍要给出确定意图。"""

    def boom(*a, **k):
        raise AssertionError("click_star 不该调用 LLM")

    monkeypatch.setattr("src.vc.graph.nodes.router.get_llm_provider", boom)
    out = intent_router(dict(STAR_STATE, query_raw=""))
    assert out["intent"] == "METRIC"
    assert out["intent_confidence"] == 1.0
    assert out["filters_strict"] is True         # ui_filters 作为绝对过滤条件
    assert out["route_cfg"]["source"] == "ui_star"


def test_router_flags_unknown_star_node():
    out = intent_router({"ui_action": UI_ACTION_STAR, "ui_filters": {"node_id": "not_a_node"}})
    assert out["degraded"] == ["ui:unknown_star_node"]
    assert out["intent"]                          # 未知节点不阻断，降级继续


def test_generator_emits_structured_ui_payload():
    ctx = [{"page": 10, "text": "毛利率为 41.2%，同比提升 1.8 个百分点", "chunk_id": "c1"}]
    out = generator({
        "ui_action": UI_ACTION_STAR,
        "star_node_id": "gross_margin",
        "unlocked_nodes": ["revenue"],
        "context": ctx,
        "query_rewritten": "毛利率",
    })
    payload = out["ui_payload"]
    assert payload["node_id"] == "gross_margin"
    assert len(payload["explanation"]) <= 100
    # MockLLM 不支持 JSON，应回落抽取式但依然给出可渲染载荷
    assert isinstance(payload["citations"], list) and isinstance(payload["unlock_next"], list)
    assert out["answer"]                          # 文本仍写入，供 gate/validate 复用


def test_unlock_accumulates_across_turns_and_resets():
    acc: list = []
    for node in ("revenue", "gross_margin", "revenue"):
        patch = unlock_commit({"star_node_id": node, "unlocked_nodes": acc})
        acc = merge_unique(acc, patch.get("unlocked_nodes") or [])
    assert acc == ["revenue", "gross_margin"]     # 去重累加，跨轮只增

    patch = unlock_commit({"ui_action": UI_ACTION_RESET, "unlocked_nodes": acc})
    assert merge_unique(acc, patch["unlocked_nodes"]) == []
    assert patch["ui_payload"]["reset"] is True


def test_fallback_returns_ui_payload_for_star_click():
    out = fallback_node({
        "ui_action": UI_ACTION_STAR,
        "star_node_id": "cashflow",
        "errors": [{"code": "E_EMPTY_RECALL"}],
        "context": [],
        "unlocked_nodes": ["revenue"],
    })
    assert out["ui_payload"]["refused"] is True
    assert out["ui_payload"]["node_id"] == "cashflow"
    assert "net_profit" in out["ui_payload"]["unlock_next"]


def test_query_graph_compiles_with_checkpointer():
    graph = build_query_graph()
    assert graph.checkpointer is not None         # unlocked_nodes 靠它跨轮持久化


def test_reset_action_skips_retrieval():
    assert route_after_rewrite({"ui_action": UI_ACTION_RESET}) == "unlock_commit"
    assert route_after_rewrite({"ui_action": UI_ACTION_QUERY}) == "intent_router"


def test_add_list_reset_sentinel_is_filtered():
    assert add_list(["a"], RESET_LIST) == []                       # 哨兵清空上一轮
    assert add_list(["a"], RESET_LIST + ["b"]) == ["b"]            # 只留本轮
    assert add_list(["a"], ["b"]) == ["a", "b"]                    # 常规累加语义不变


def test_guard_downgrades_ui_payload_when_numbers_have_no_source():
    """卡片渲染的是 explanation，正文被降级时卡片必须同步，否则 UI 露出未过闸门的原文。"""
    out = output_guard({
        "ui_action": UI_ACTION_STAR,
        "answer": "毛利率 41.2%",
        "valid": False,
        "citations": [],
        "context": [{"page": 10, "text": "毛利率为 41.2%", "chunk_id": "c1"}],
        "ui_payload": {"node_id": "gross_margin", "explanation": "毛利率 41.2%[P10]", "citations": ["P10"],
                       "unlocked_nodes": ["gross_margin"]},
    })
    payload = out["ui_payload"]
    assert "%" not in payload["explanation"]        # 无源数字不得留在卡片上
    assert payload["refused"] is True
    assert payload["citations"] == []
    assert payload["disclaimer"]                    # 免责声明以独立字段下发，不占 100 字
    assert payload["unlocked_nodes"] == ["gross_margin"]   # unlock_commit 写入的内容不被冲掉


def test_guard_syncs_verified_citations_into_ui_payload():
    out = output_guard({
        "ui_action": UI_ACTION_STAR,
        "answer": "毛利率同比提升[P10]",
        "valid": True,
        "citations": [{"page": 10}],
        "ui_payload": {"node_id": "gross_margin", "explanation": "毛利率同比提升[P10]", "citations": ["P99"]},
    })
    assert out["ui_payload"]["citations"] == ["P10"]   # 编造的 P99 被校验后的页码覆盖
    assert out["ui_payload"]["explanation"]            # 通过闸门时解释文本保持原样
