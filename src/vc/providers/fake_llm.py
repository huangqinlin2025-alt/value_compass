"""FakeLLM：无 API Key 时的契约测试替身（LLM_PROVIDER=fake）。

用途：让「Prompt + Pydantic Schema + 解析校验 + 修复重试 + 降级」这条链路
在没有密钥、不联网的前提下被完整测试。

脚本（script）按节点 key 组织，值可以是：
- str：直接作为模型输出（合法 JSON / 脏输出 / markdown 围栏 / 前后废话 均可）；
- list[str|Exception]：按调用顺序消费，用完后重复最后一项；
- Exception：抛出（模拟超时、限流、解析失败）；
- callable(messages) -> str：动态生成；
- None / 缺省：返回该 Schema 的合法默认样例。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from .llm import LLMProvider

_EXAMPLES: Dict[str, str] = {
    "IntentResult": '{"intent": "QUALITATIVE", "confidence": 0.6, "reason": "fake default", "filters": {}}',
    "FaithfulnessResult": '{"passed": true, "mismatched_numbers": [], "bad_citations": [],'
                          ' "unsupported_claims": [], "reason": "fake default"}',
    "AnswerResult": '{"answer": "（FakeLLM）未配置脚本，返回默认样例。", "citations": [],'
                    ' "refused": false, "used_chunk_ids": []}',
}


class FakeLLM(LLMProvider):
    name = "fake"
    supports_json = True

    def __init__(self, script: Optional[Dict[str, Any]] = None):
        self.script: Dict[str, Any] = dict(script or {})
        self.calls: List[Dict[str, Any]] = []  # 记录调用，便于测试断言
        self._key = ""
        self._schema = ""

    # ---------- 脚本管理 ----------
    def set_script(self, script: Dict[str, Any]) -> "FakeLLM":
        self.script = dict(script or {})
        return self

    def reset(self) -> "FakeLLM":
        self.calls = []
        return self

    def _next(self, key: str) -> Any:
        slot = key if key in self.script else ("default" if "default" in self.script else "")
        if not slot:
            return None
        item = self.script[slot]
        if isinstance(item, list):
            if not item:
                return None
            if len(item) > 1:
                self.script[slot] = item[1:]
            return item[0]
        return item

    # ---------- Provider 接口 ----------
    def generate(self, prompt: str, *, timeout: float = None) -> str:
        return self.chat([{"role": "user", "content": prompt}], timeout=timeout)

    def chat(self, messages: List[Dict[str, str]], *, timeout: float = None, json_mode: bool = False) -> str:
        key = self._key or "default"
        self.calls.append({"key": key, "json_mode": bool(json_mode), "messages": len(messages or [])})
        item = self._next(key)
        if callable(item):
            item = item(messages)
        if isinstance(item, Exception):
            raise item
        if item is None:
            return _EXAMPLES.get(self._schema, "")
        return str(item)

    def generate_json(
        self,
        messages: List[Dict[str, str]],
        schema_model: Type[Any],
        *,
        key: str = "",
        retries: int = None,
        timeout: float = None,
    ) -> Any:
        self._key = key
        self._schema = getattr(schema_model, "__name__", "")
        try:
            return LLMProvider.generate_json(
                self, messages, schema_model, key=key, retries=retries, timeout=timeout
            )
        finally:
            self._key = ""
            self._schema = ""

    def answer(self, query: str, chunks: List[Dict[str, Any]], intent: str = "", *, timeout: float = None) -> str:
        return "（FakeLLM 文本模式）问题：%s" % query


def make_fake(script: Dict[str, Any] = None) -> FakeLLM:
    return FakeLLM(script)


__all__ = ["FakeLLM", "make_fake"]
