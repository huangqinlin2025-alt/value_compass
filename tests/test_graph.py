"""图与容错单测：路由、数值闸门、兜底、safe_node 容错、图可编译。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.config import CONFIG
from src.vc.decorators import safe_node
from src.vc.errors import ErrorCode
from src.vc.graph.nodes.fallback import fallback_node
from src.vc.graph.nodes.gate import faithfulness_gate
from src.vc.graph.nodes.router import classify, intent_router
from src.vc.graph.query_graph import build_query_graph
from src.vc.providers.llm import MockLLM


def test_classify_routes():
    assert classify("这只股票明天能买吗？")["OOS"] > 0
    assert classify("你好")["CHITCHAT"] > 0
    assert classify("总结一下这份报告")["SUMMARY"] > 0
    assert classify("公司本期营业收入是多少？")["METRIC"] > 0


def test_router_short_circuit_oos():
    out = intent_router({"query_rewritten": "推荐一只股票", "query_raw": "推荐一只股票"})
    assert out["intent"] == "OOS"


def test_router_unclear_for_gibberish():
    out = intent_router({"query_rewritten": "嗯", "query_raw": "嗯"})
    assert out["intent"] in ("UNCLEAR", "QUALITATIVE")


def test_faithfulness_gate_accepts_numbers_from_context():
    state = {
        "draft_answer": "营业收入 18.2 亿元 [P10]",
        "context": [{"page": 10, "text": "报告期内，公司实现营业收入 18.2 亿元"}],
    }
    out = faithfulness_gate(state)
    assert out["gate_pass"] is True


def test_faithfulness_gate_rejects_hallucinated_number():
    state = {
        "draft_answer": "营业收入 99.9 亿元 [P10]",
        "context": [{"page": 10, "text": "报告期内，公司实现营业收入 18.2 亿元"}],
    }
    out = faithfulness_gate(state)
    assert out["gate_pass"] is False
    assert out["errors"][0]["code"] == ErrorCode.E_NUM_MISMATCH.value


def test_faithfulness_gate_rejects_unknown_page():
    state = {
        "draft_answer": "见附注 [P999]",
        "context": [{"page": 10, "text": "报告期内，公司实现营业收入 18.2 亿元"}],
    }
    out = faithfulness_gate(state)
    assert out["gate_pass"] is False
    assert out["errors"][0]["code"] == ErrorCode.E_CITATION_MISS.value


def test_gate_ignores_citation_page_numbers():
    """引用标记 [P12] 里的页码不能被当成"答案里的数值"。"""
    state = {"draft_answer": "见 [P12]", "context": [{"page": 12, "text": "无数字内容"}]}
    assert faithfulness_gate(state)["gate_pass"] is True


def test_fallback_never_returns_empty():
    out = fallback_node({"errors": [{"code": "E_EMPTY_RECALL"}], "context": []})
    assert out["final_answer"]
    assert CONFIG.disclaimer in out["final_answer"]
    assert out["fallback_reason"] == "empty"


def test_fallback_includes_excerpt_when_low_score():
    ctx = [{"page": 7, "text": "营业收入 1,000 元"}]
    out = fallback_node({"errors": [{"code": "E_LOW_SCORE"}], "context": ctx})
    assert "P7" in out["final_answer"]


def test_safe_node_captures_exception_instead_of_raising():
    @safe_node("boom", timeout=1.0, retries=0, fallback_patch={"answer": ""})
    def boom(state):
        raise RuntimeError("故意失败")

    out = boom({})
    assert out["answer"] == ""
    assert out["errors"][0]["node"] == "boom"
    assert out["degraded"] == ["boom"]
    assert out["trace"][0]["ok"] is False


def test_safe_node_timeout_is_captured():
    import time

    @safe_node("slow", timeout=0.3, retries=0)
    def slow(state):
        time.sleep(2)
        return {}

    out = slow({})
    assert out["errors"][0]["code"] == ErrorCode.E_TIMEOUT.value


def test_mock_llm_is_extractive():
    ctx = [{"page": 5, "text": "营业收入 1,815,972,101 元，同比减少 3.90%", "company": "测试公司"}]
    ans = MockLLM().answer("营业收入是多少？", ctx)
    assert "1,815,972,101" in ans or "1815972101" in ans.replace(",", "")
    assert "[P5]" in ans


def test_query_graph_compiles():
    graph = build_query_graph()
    assert graph is not None
