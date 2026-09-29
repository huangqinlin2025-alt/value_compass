"""LLM 契约链路单测（无 API Key 也能跑）：解析 / 校验 / 修复重试 / 降级 / 节点接入。

用 FakeLLM 注入脚本化响应，覆盖真实模型会遇到的全部脏输出形态。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.config import CONFIG  # noqa: E402
from src.vc.errors import ErrorCode, VCException  # noqa: E402
from src.vc.graph.nodes.gate import faithfulness_gate  # noqa: E402
from src.vc.graph.nodes.generate import generator, sanitize_answer  # noqa: E402
from src.vc.graph.nodes.router import intent_router  # noqa: E402
from src.vc.providers.fake_llm import FakeLLM  # noqa: E402
from src.vc.providers.llm import MockLLM  # noqa: E402
from src.vc.schema import AnswerResult, FaithfulnessResult, IntentResult  # noqa: E402


def test_fake_llm_returns_valid_json():
    fake = FakeLLM({"router": '{"intent": "COMPARE", "confidence": 0.8, "reason": "同比"}'})
    obj = fake.generate_json([{"role": "user", "content": "q"}], IntentResult, key="router")
    assert obj.intent == "COMPARE" and obj.confidence == 0.8


def test_fake_llm_tolerates_fenced_output():
    fake = FakeLLM({"gate": '```json\n{"passed": true, "reason": "ok"}\n```'})
    obj = fake.generate_json([{"role": "user", "content": "q"}], FaithfulnessResult, key="gate")
    assert obj.passed is True


def test_repair_retry_uses_second_attempt():
    # 第一次输出不可解析 -> 自动带错误信息修复重试 -> 第二次通过
    fake = FakeLLM({"gate": ["这不是JSON", '{"passed": false, "reason": "修正后"}']})
    obj = fake.generate_json([{"role": "user", "content": "q"}], FaithfulnessResult, key="gate")
    assert obj.passed is False
    assert len(fake.calls) == 2  # 证明确实发生了修复重试


def test_bad_json_raises_when_no_repair():
    fake = FakeLLM({"router": "我拒绝输出 JSON"})
    try:
        fake.generate_json([{"role": "user", "content": "q"}], IntentResult, key="router", retries=0)
        assert False, "应当抛出 E_LLM_BADJSON"
    except VCException as exc:
        assert exc.code == ErrorCode.E_LLM_BADJSON


def test_mock_llm_does_not_claim_json_support():
    assert MockLLM().supports_json is False  # 节点据此直接走确定性逻辑，不产生噪声错误


def test_router_uses_llm_when_rule_unsure(monkeypatch):
    fake = FakeLLM({"router": '{"intent": "TABLE", "confidence": 0.9, "reason": "问的是构成"}'})
    monkeypatch.setattr("src.vc.graph.nodes.router.get_llm_provider", lambda **kw: fake)
    monkeypatch.setattr(CONFIG, "router_llm_enabled", True)

    # 规则对这句话犹豫（多意图并列）-> 交给 LLM
    out = intent_router({"query_rewritten": "各项业务分别是什么情况", "query_raw": "各项业务分别是什么情况"})
    assert out["intent"] == "TABLE"
    assert out["route_cfg"]["source"] == "llm"


def test_router_rule_wins_when_confident(monkeypatch):
    fake = FakeLLM({"router": '{"intent": "QUALITATIVE", "confidence": 0.9}'})
    monkeypatch.setattr("src.vc.graph.nodes.router.get_llm_provider", lambda **kw: fake)
    monkeypatch.setattr(CONFIG, "router_llm_enabled", True)

    out = intent_router({"query_rewritten": "这只股票明天能买吗", "query_raw": "这只股票明天能买吗"})
    assert out["intent"] == "OOS"  # 合规红线不被 LLM 推翻


def test_router_falls_back_to_rule_on_llm_failure(monkeypatch):
    def _boom(**kw):
        raise VCException(ErrorCode.E_LLM_TIMEOUT, "boom")

    monkeypatch.setattr("src.vc.graph.nodes.router.get_llm_provider", _boom)
    monkeypatch.setattr(CONFIG, "router_llm_enabled", True)

    # 规则犹豫（TABLE/QUALITATIVE 并列）-> 本该问 LLM -> 调用失败 -> 回落规则且标记降级
    q = "各项业务分别是什么情况"
    out = intent_router({"query_rewritten": q, "query_raw": q})
    assert out["intent"] == "TABLE"
    assert any("router:llm_fallback_rule" in d for d in out.get("degraded", []))


def test_router_skips_llm_when_provider_has_no_json(monkeypatch):
    monkeypatch.setattr("src.vc.graph.nodes.router.get_llm_provider", lambda **kw: MockLLM())
    monkeypatch.setattr(CONFIG, "router_llm_enabled", True)

    q = "各项业务分别是什么情况"
    out = intent_router({"query_rewritten": q, "query_raw": q})
    assert out["route_cfg"]["source"] == "rule"  # Mock 不支持 JSON -> 不打扰，也不写降级
    assert not out.get("degraded")


def test_gate_blocks_fabricated_number():
    state = {
        "query_raw": "本期营业收入是多少？",
        "draft_answer": "本期营业收入为 999.99 亿元 [P6]",
        "context": [{"page": 6, "chunk_id": "c1", "text": "本期营业收入 18.2 亿元"}],
    }
    out = faithfulness_gate(state)
    assert out["gate_pass"] is False
    assert "999.99" in out["gate_reason"]
    assert out["errors"][0]["code"] == ErrorCode.E_NUM_MISMATCH.value


def test_gate_blocks_fake_page():
    state = {
        "query_raw": "营业收入？",
        "draft_answer": "营业收入 18.2 亿元 [P99]",
        "context": [{"page": 6, "chunk_id": "c1", "text": "本期营业收入 18.2 亿元"}],
    }
    out = faithfulness_gate(state)
    assert out["gate_pass"] is False
    assert out["errors"][0]["code"] == ErrorCode.E_CITATION_MISS.value


def test_gate_llm_can_block_unsupported_claim(monkeypatch):
    fake = FakeLLM({
        "gate": '{"passed": false, "unsupported_claims": ["资料未提及利润大幅提升"], "reason": "结论越界"}'
    })
    monkeypatch.setattr("src.vc.graph.nodes.gate.get_llm_provider", lambda **kw: fake)
    monkeypatch.setattr(CONFIG, "gate_llm_enabled", True)

    state = {
        "query_raw": "净利润？",
        "draft_answer": "净利润 20 亿元，因此利润大幅提升 [P6]",  # 数字与页码都对，结论越界
        "context": [{"page": 6, "chunk_id": "c1", "text": "净利润 20 亿元"}],
    }
    out = faithfulness_gate(state)
    assert out["gate_pass"] is False
    assert "无依据断言" in out["gate_reason"]


def test_generator_sanitizes_invalid_citations():
    result = AnswerResult.model_validate_json(
        '{"answer": "收入 18.2 亿元 [P6]，另有 [P99]", "citations": [{"page": 6}, {"page": 99}]}'
    )
    text = sanitize_answer(result, [{"page": 6, "text": "收入 18.2 亿元"}])
    assert "[P6]" in text and "[P99]" not in text  # 编造的引用被剥离


def test_generator_uses_json_contract(monkeypatch):
    fake = FakeLLM({
        "generator": '{"answer": "营业收入 18.2 亿元 [P6]", '
                     '"citations": [{"page": 6, "quote": "营业收入 18.2 亿元"}], '
                     '"used_chunk_ids": ["c1"]}'
    })
    monkeypatch.setattr("src.vc.graph.nodes.generate.get_llm_provider", lambda **kw: fake)
    monkeypatch.setattr(CONFIG, "generator_json_enabled", True)

    out = generator({
        "query_raw": "营业收入是多少？",
        "intent": "METRIC",
        "context": [{"page": 6, "chunk_id": "c1", "text": "营业收入 18.2 亿元"}],
    })
    assert "18.2" in out["answer"] and "[P6]" in out["answer"]
    assert out["generation_used"] == ["c1"]


def test_generator_falls_back_when_json_broken(monkeypatch):
    fake = FakeLLM({"generator": "拒绝输出 JSON"})
    monkeypatch.setattr("src.vc.graph.nodes.generate.get_llm_provider", lambda **kw: fake)
    monkeypatch.setattr(CONFIG, "generator_json_enabled", True)

    out = generator({
        "query_raw": "营业收入是多少？",
        "intent": "METRIC",
        "context": [{"page": 6, "chunk_id": "c1", "text": "营业收入 18.2 亿元"}],
    })
    assert out["answer"]  # 回落到文本模式，答案不为空
    assert any("generator:json_fallback_text" in d for d in out.get("degraded", []))
