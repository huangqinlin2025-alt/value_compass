"""幻觉探针：主动让 generator 输出编造的财报数字，检查防线是否真的拦得住。

验证链路：generator(伪造数字) -> faithfulness_gate 拦截 -> retry_shrink 收紧上下文
-> 再次生成仍伪造 -> 转 fallback（绝不把假数字输出给用户）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..graph.nodes import generate as generate_mod
from ..graph.query_graph import ask
from ..providers.fake_llm import FakeLLM

FABRICATED_ANSWER = '{"answer": "本期营业收入为 8888.88 亿元 [P6]，同比增长 66.66% [P6]", ' \
                    '"citations": [{"page": 6, "quote": ""}], "refused": false, "used_chunk_ids": []}'

# 伪造页码（数字是真的，页码是编的）
FAKE_PAGE_ANSWER = '{"answer": "本期营业收入为 18.2 亿元 [P99]", ' \
                   '"citations": [{"page": 99, "quote": ""}], "refused": false, "used_chunk_ids": []}'


def probe_fabricated_number(query: str = "公司本期营业收入是多少？", scripted: str = None) -> Dict[str, Any]:
    """注入一次伪造输出，返回拦截结果。"""
    fake = FakeLLM({"generator": scripted or FABRICATED_ANSWER})
    origin = generate_mod.get_llm_provider
    generate_mod.get_llm_provider = lambda **kw: fake  # type: ignore[assignment]
    try:
        result = ask(query)
    finally:
        generate_mod.get_llm_provider = origin  # type: ignore[assignment]

    answer = result.get("final_answer", "") or ""
    return {
        "query": query,
        "blocked": not result.get("gate_pass", True),   # gate 判伪 -> True 表示拦住了
        "retry_count": int(result.get("retry_count") or 0),
        "fallback_reason": result.get("fallback_reason") or "",
        "leaked": "8888.88" in answer or "66.66" in answer or "[P99]" in answer,
        "answer": answer[:120],
        "errors": [e.get("code") for e in (result.get("errors") or [])],
    }


def run_probes() -> List[Dict[str, Any]]:
    return [
        probe_fabricated_number(),
        probe_fabricated_number("资产负债率是多少？", FAKE_PAGE_ANSWER),
    ]


def assert_no_leak(probes: List[Dict[str, Any]]) -> None:
    """供测试与 CI 使用：任何一条漏出伪造内容都应失败。"""
    for p in probes:
        assert p["blocked"], "数值闸门未拦截伪造内容: %s" % p["query"]
        assert p["retry_count"] >= 1, "未触发 retry_shrink: %s" % p["query"]
        assert not p["leaked"], "伪造内容泄漏到最终答案: %s -> %s" % (p["query"], p["answer"])
