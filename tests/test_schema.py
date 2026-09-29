"""契约层单测：Schema 容错、脏输出解析、默认值兜底。

这些用例保证"模型输出脏一点，系统也不崩"：枚举写错、置信度越界、
输出带 markdown 围栏、缺字段，都要被收敛成合法对象或直接判为解析失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.schema import (  # noqa: E402
    AnswerResult,
    FaithfulnessResult,
    IntentResult,
    extract_json_object,
    validate_model,
)


def test_intent_coerce_case_and_alias():
    obj = IntentResult.model_validate_json('{"intent": "metric", "confidence": 1.5}')
    assert obj.intent == "METRIC"
    assert obj.confidence == 1.0  # 越界收敛到 [0,1]

    obj2 = IntentResult.model_validate_json('{"intent": "RETRIEVAL", "confidence": 0.6}')
    assert obj2.intent == "QUALITATIVE"  # 同义词映射

    obj3 = IntentResult.model_validate_json('{"intent": "胡说八道", "confidence": 0.6}')
    assert obj3.intent == "UNCLEAR"  # 未知标签 -> UNCLEAR，不抛异常


def test_intent_filters_default():
    obj = IntentResult.model_validate_json('{"intent": "METRIC", "confidence": 0.9, "filters": null}')
    assert obj.filters == {}


def test_faithfulness_alias_and_coercion():
    obj = FaithfulnessResult.model_validate_json('{"pass": false, "bad_citations": ["7", "x"]}')
    assert obj.passed is False
    assert obj.bad_citations == [7]  # 字符串页码转 int，非法值丢弃


def test_answer_defaults():
    obj = AnswerResult.model_validate_json('{"answer": "营业收入 18.2 亿元 [P6]"}')
    assert obj.citations == [] and obj.used_chunk_ids == [] and obj.refused is False


def test_answer_requires_answer_field():
    assert validate_model(AnswerResult, '{"citations": []}') is None


def test_extract_json_object_tolerates_fences_and_noise():
    raw = '好的，结果如下：\n```json\n{"answer": "hi"}\n```\n希望有帮助'
    assert extract_json_object(raw) == '{"answer": "hi"}'

    assert extract_json_object('前缀 {"a": 1} 后缀') == '{"a": 1}'
    assert extract_json_object('完全没有 JSON') == ''


def test_validate_model_garbage():
    assert validate_model(IntentResult, "我不是 JSON") is None
    assert validate_model(IntentResult, "") is None
    assert validate_model(IntentResult, '{"intent": "METRIC", "confidence": 0.9}') is not None
