"""评测层单测：指标口径（幻觉判定）与 CFQA 适配。

这里锁的是"度量本身不能撒谎"——兜底清空 context、摘录截断、问题自带年份
这三种情况都曾被误判成幻觉，导致调参结论跑偏。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.eval.runner import evaluate  # noqa: E402


def _result(answer: str, ctx=None, fused=None, fallback: str = "") -> dict:
    return {
        "final_answer": answer,
        "context": ctx if ctx is not None else [],
        "reranked": [],
        "fused": fused if fused is not None else [],
        "citations": [],
        "fallback_reason": fallback,
        "gate_pass": True,
        "retry_count": 0,
        "degraded": [],
    }


def test_fabricate_detected_when_number_not_in_context():
    case = {"q": "营业收入是多少？", "expect_keywords": ["营业收入"]}
    row = evaluate(case, _result("营业收入 99.9 亿元", ctx=[{"page": 1, "text": "营业收入 18.2 亿元"}]))
    assert row["fabricated"] is True
    assert "99.9" in "".join(row["missing_numbers"])


def test_fallback_excerpt_from_fused_is_not_fabrication():
    """兜底会清空 context，但摘录来自 fused——不能整段判成幻觉。"""
    case = {"q": "营业收入是多少？", "expect_keywords": ["营业收入"]}
    fused = [{"page": 3, "text": "营业收入 18.2 亿元"}]
    row = evaluate(case, _result("未找到依据，摘录：营业收入 18.2 亿元", ctx=[], fused=fused,
                                 fallback="low_score"))
    assert row["fabricated"] is False
    assert row["is_fallback"] is True


def test_truncated_number_in_excerpt_is_not_fabrication():
    """摘录末尾 "...20…" 是截断产物，不是模型编的数字。"""
    case = {"q": "长期股权投资有多少？"}
    fused = [{"page": 71, "text": "长期股权投资 2026 年 6 月 30 日 207,000,000"}]
    row = evaluate(case, _result("摘录：2026 年 6 月 30 日 20…", ctx=[], fused=fused,
                                 fallback="low_score"))
    assert row["fabricated"] is False


def test_year_from_question_is_not_fabrication():
    """兜底话术回述用户问题里的年份，不算幻觉。"""
    case = {"q": "2022年科技创新具体体现在哪里", "mode": "no-corpus"}
    row = evaluate(case, _result("未检索到与 2022年科技创新 相关的依据。", ctx=[], fused=[],
                                 fallback="low_score"))
    assert row["fabricated"] is False
    assert row["honest"] is True


def test_no_corpus_mode_hit_means_honest():
    case = {"q": "某公司营收是多少", "mode": "no-corpus"}
    honest = evaluate(case, _result("未在报告中检索到可靠依据。", fallback="low_score"))
    assert honest["hit"] is True
    assert honest["honest"] is True

    hard = evaluate(case, _result("营业收入为 12.3 亿元 [P10]",
                                  ctx=[{"page": 10, "text": "营业收入 12.3 亿元"}]))
    assert hard["hit"] is False
    assert hard["honest"] is False
