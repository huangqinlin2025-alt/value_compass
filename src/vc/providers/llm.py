"""LLM 适配器。

- MockLLM（默认）：不联网，基于检索片段做**抽取式**作答，保证答案中的每个数字都能在
  原文中逐字命中；它同时是"生成失败时的兜底生成器"。
- OpenAICompatLLM：DeepSeek / 通义 / 智谱 / OpenAI 同协议，仅从环境变量读 Key。

两者共用 answer(query, chunks, intent) 结构化接口，切换对上层无感。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

from ..config import CONFIG, get_api_key
from ..errors import ErrorCode, VCException
from ..schema import schema_json, validate_model
from ..text_utils import clip, coverage_score, normalize_text, token_set

_CITATION_RE = re.compile(r"\[P(\d+)\]")
_INLINE_SOURCE_RE = re.compile(r"(?:来源|出处|参见)\s*[:：]\s*((?:P?\d+)(?:\s*[、,，/]\s*P?\d+)*)")

REPAIR_TEMPLATE = """上一次输出没有通过 JSON Schema 校验。
错误信息：{error}
你的原始输出：
{raw}

请重新输出**单个 JSON 对象**，字段必须与下面的 Schema 完全一致；不要输出解释、不要输出 markdown 围栏：
{schema}
"""

SYSTEM_PROMPT = """你是金融财报分析助手。只依据给定的<资料>回答，不得引入外部知识。
规则：
1. 每个结论后用 [P页码] 标注来源，页码只能来自资料中出现的页码。
2. 资料中没有的信息，直接回答"未在报告中找到相关信息"，禁止推测。
3. 不得给出买卖建议、目标价或收益承诺。
4. 数值必须与资料原文完全一致，不得换算、四舍五入或估算。
"""


class LLMProvider:
    name = "base"
    # 是否支持结构化输出：Mock 不支持 -> 节点直接走确定性逻辑，不产生噪声错误
    supports_json = False

    def generate(self, prompt: str, *, timeout: float = None) -> str:
        raise NotImplementedError

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        timeout: float = None,
        json_mode: bool = False,
    ) -> str:
        """多轮消息接口；默认把 messages 拼成单轮 prompt 交给 generate。"""
        prompt = "\n\n".join((m or {}).get("content", "") for m in messages or [])
        return self.generate(prompt, timeout=timeout)

    def generate_json(
        self,
        messages: List[Dict[str, str]],
        schema_model: Type[BaseModel],
        *,
        key: str = "",
        retries: Optional[int] = None,
        timeout: float = None,
    ) -> BaseModel:
        """强约束的结构化调用：解析 -> Pydantic 校验 -> 修复重试 -> 仍失败抛 E_LLM_BADJSON。

        调用方（节点）负责捕获异常并回落到确定性逻辑，图不会因一次脏输出而断。
        """
        repair = int(CONFIG.llm_json_repair if retries is None else retries)
        raw = self.chat(messages, timeout=timeout, json_mode=True)
        obj = validate_model(schema_model, raw)
        if obj is not None:
            return obj

        last_raw, last_err = raw, "无法从输出中解析出 JSON 对象"
        for _ in range(max(0, repair)):
            fix_msg = REPAIR_TEMPLATE.format(
                error=last_err,
                raw=(last_raw or "")[:2000],
                schema=schema_json(schema_model),
            )
            try:
                raw2 = self.chat(
                    list(messages or []) + [{"role": "user", "content": fix_msg}],
                    timeout=timeout,
                    json_mode=True,
                )
            except VCException:
                raise
            except Exception as exc:  # 兜底：非业务异常归一化为超时
                raise VCException(ErrorCode.E_LLM_TIMEOUT, "修复重试调用失败: %s" % exc)
            obj = validate_model(schema_model, raw2)
            if obj is not None:
                return obj
            last_raw, last_err = raw2, "修复后的输出仍不符合 Schema"
        raise VCException(
            ErrorCode.E_LLM_BADJSON,
            "JSON Schema 校验失败(%s): %s" % (getattr(schema_model, "__name__", "?"), last_err),
        )

    def answer(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
        intent: str = "",
        *,
        timeout: float = None,
    ) -> str:
        raise NotImplementedError


class MockLLM(LLMProvider):
    """抽取式作答：从 context 中挑出覆盖度最高且含数字的句子拼接。"""

    name = "mock"

    def generate(self, prompt: str, *, timeout: float = None) -> str:
        # Mock 不做自由生成，直接从 prompt 中截取 <资料> 段落的首句作为"答案"
        m = re.search(r"<资料>(.*?)</资料>", prompt, re.S)
        body = (m.group(1) if m else prompt).strip()
        first = re.split(r"[。；\n]", body)
        return (first[0] + "。") if first and first[0] else "未在报告中找到相关信息。"

    def answer(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
        intent: str = "",
        *,
        timeout: float = None,
    ) -> str:
        if not chunks:
            return "未在报告中找到相关信息。"
        # 关键：尊重上游（RRF + rerank）已经排好的顺序，Mock 只负责"抽句"，
        # 不再二次排序，否则会把重排的努力抵消掉。
        q_terms = token_set(query)
        picked = []
        for c in chunks[:3]:
            sent = _best_sentence(c.get("text", ""), q_terms)
            if sent:
                picked.append((c, sent))
        if not picked:
            c = chunks[0]
            picked = [(c, clip(c.get("text", ""), 160))]

        lines = ["%s [P%s]" % (sent, c.get("page", 0)) for c, sent in picked]
        company = picked[0][0].get("company") or ""
        head = "根据《%s》：\n" % company if company else ""
        return head + "\n".join("- " + x for x in lines)


class OpenAICompatLLM(LLMProvider):
    name = "openai_compat"
    supports_json = True

    def __init__(self, model: str = None, base_url: str = None, api_key_env: str = "OPENAI_API_KEY"):
        import requests  # 延迟导入

        self._requests = requests
        self.model = model or CONFIG.llm_model
        self.base_url = (base_url or CONFIG.llm_base_url).rstrip("/")
        self.api_key = get_api_key(api_key_env)
        if not self.api_key:
            raise VCException(ErrorCode.E_LLM_BADJSON, "缺少环境变量 %s" % api_key_env)

    def _post(self, payload: Dict[str, Any], timeout: float) -> str:
        try:
            resp = self._requests.post(
                self.base_url + "/chat/completions",
                headers={"Authorization": "Bearer %s" % self.api_key},
                json=payload,
                timeout=timeout,
            )
        except Exception as exc:
            raise VCException(ErrorCode.E_LLM_TIMEOUT, "调用 LLM 失败: %s" % exc)
        if resp.status_code == 429:
            raise VCException(ErrorCode.E_LLM_RATE_LIMIT, "LLM 限流")
        if resp.status_code >= 400:
            raise VCException(ErrorCode.E_LLM_BADJSON, "LLM 返回 %s" % resp.status_code)
        try:
            return (resp.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            raise VCException(ErrorCode.E_LLM_BADJSON, "LLM 返回结构无法解析")

    def generate(self, prompt: str, *, timeout: float = None) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        return self._post(payload, timeout or CONFIG.timeout_llm)

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        timeout: float = None,
        json_mode: bool = False,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": list(messages or []),
            "temperature": 0.0 if json_mode else 0.1,
        }
        if json_mode and CONFIG.llm_json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            return self._post(payload, timeout or CONFIG.timeout_llm)
        except VCException:
            # 部分 OpenAI 兼容端点不支持 response_format，去掉后重试一次
            if payload.get("response_format"):
                payload.pop("response_format", None)
                return self._post(payload, timeout or CONFIG.timeout_llm)
            raise

    def answer(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
        intent: str = "",
        *,
        timeout: float = None,
    ) -> str:
        blocks = []
        for c in chunks:
            blocks.append("[P%s] %s" % (c.get("page", 0), c.get("text", "")))
        prompt = (
            "<资料>\n" + "\n\n".join(blocks) + "\n</资料>\n\n"
            "问题意图：%s\n用户问题：%s\n请用中文回答，并在句末标注 [P页码]。" % (intent or "通用", query)
        )
        return self.generate(prompt, timeout=timeout)


def _best_sentence(text: str, q_terms: set) -> str:
    text = normalize_text(text)
    if not text:
        return ""
    parts = re.split(r"(?<=[。；\n])", text)
    # 去掉 "[P12|章节路径]" 这类仅用于检索与溯源的前缀行
    parts = [p.strip() for p in parts if p.strip() and not p.strip().startswith("[P")]
    if not parts:
        return text[:120]
    best, best_score = parts[0], -1.0
    for p in parts[:12]:
        score = coverage_score(q_terms, p)
        if re.search(r"\d", p):
            score += 0.2
        if score > best_score:
            best, best_score = p, score
    return best[:200]


_CACHE: dict = {}


def get_llm_provider(allow_fallback: bool = True) -> LLMProvider:
    key = CONFIG.llm_provider
    if key in _CACHE:
        return _CACHE[key]
    provider: Optional[LLMProvider] = None
    try:
        if key == "fake":
            from .fake_llm import FakeLLM

            provider = FakeLLM()
        elif key == "openai_compat":
            provider = OpenAICompatLLM()
        else:
            provider = MockLLM()
    except Exception:
        if not allow_fallback:
            raise
        provider = MockLLM()
    _CACHE[key] = provider
    return provider


def extract_citations(answer: str) -> List[int]:
    """抽取引用页码：标准 [P12] 角标，外加「来源：P10、P12」这类常见变体。

    为什么必须兼容变体：模型（尤其小模型）常把角标写成"（来源:P10、P12）"，
    而 sanitize 补引时生成的也是"来源：P10"；只认 [Pxx] 会把这些真实溯源
    判成"答案含数字但无任何引用"，进而被 output_guard 整段改成兜底文案。

    变体要求带「来源/出处/参见」前缀才放行，避免把 P2P 之类噪声误当页码——
    白名单只做二次过滤，抽错一次就会让闸门误杀一次。
    """
    text = answer or ""
    pages = [int(x) for x in _CITATION_RE.findall(text)]
    for m in _INLINE_SOURCE_RE.finditer(text):
        pages.extend(int(x) for x in re.findall(r"\d+", m.group(1)))
    return sorted(set(pages))
