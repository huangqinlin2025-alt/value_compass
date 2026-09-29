"""LLM 契约层：三大脑节点（intent_router / faithfulness_gate / generator）的 Pydantic Schema。

为什么必须有契约层：
1. 金融问答最怕"自由发挥"——用 Schema 把输出钉死成可校验的结构，越界即拒；
2. 契约是**可测试的**：无 API Key 时用 FakeLLM 灌入脏输出即可覆盖解析/校验/修复/降级全分支；
3. 节点拿到的是模型对象而不是字符串，下游（渲染角标、闸门判定）不再写正则猜测。

容错约定：
- 解析失败 -> 一次"带错误回填"的修复重试 -> 仍失败抛 VCException(E_LLM_BADJSON)，
  由各节点回落到确定性逻辑（规则路由 / 正则闸门 / 抽取式作答），图不断裂。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field, model_validator

# 意图枚举：与 router.RULES / ROUTE_CFG 保持一致
INTENT_LITERAL = "METRIC TABLE COMPARE CALC QUALITATIVE SUMMARY CHITCHAT OOS UNCLEAR".split()

# LLM 偶尔会自造同义词，收敛到枚举内，避免整条链路被一个脏标签打断
_INTENT_ALIAS = {
    "RETRIEVAL": "QUALITATIVE",
    "GENERAL": "QUALITATIVE",
    "FACT": "METRIC",
    "NUMBER": "METRIC",
    "NUMERIC": "METRIC",
    "RATIO": "CALC",
    "CALCULATION": "CALC",
    "ANALYSIS": "QUALITATIVE",
    "RISK": "QUALITATIVE",
    "OUT_OF_SCOPE": "OOS",
    "OUTOFSCOPE": "OOS",
    "REFUSE": "OOS",
    "GREETING": "CHITCHAT",
    "SMALLTALK": "CHITCHAT",
    "ABSTRACT": "SUMMARY",
    "UNKNOWN": "UNCLEAR",
    "OTHER": "UNCLEAR",
}

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


class CitationItem(BaseModel):
    """一条可溯源引用：页码必须来自资料，quote 必须能在原文中逐字命中。"""

    page: int = Field(..., ge=1, description="资料中出现过的 PDF 页码")
    quote: str = Field("", max_length=200, description="原文摘录，需与资料逐字一致")
    chunk_id: str = Field("", description="命中片段 id（可为空）")


class IntentResult(BaseModel):
    intent: str = Field(..., description="意图标签，必须为枚举之一")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0~1 置信度")
    reason: str = Field("", max_length=200, description="一句话判定依据")
    filters: Dict[str, Any] = Field(default_factory=dict, description="company/report_period/section 等过滤条件")
    refused: bool = Field(False, description="是否属于合规拒答（买卖建议/目标价等）")

    @model_validator(mode="before")
    def _coerce(cls, data: Any) -> Any:  # noqa: N805 - pydantic 约定
        if not isinstance(data, dict):
            return data
        out = dict(data)
        raw = str(out.get("intent", "") or "").strip().upper().replace("-", "_")
        out["intent"] = _INTENT_ALIAS.get(raw, raw)
        if out["intent"] not in INTENT_LITERAL:
            out["intent"] = "UNCLEAR"
        try:
            conf = float(out.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out["confidence"] = min(1.0, max(0.0, conf))
        if not isinstance(out.get("filters"), dict):
            out["filters"] = {}
        return out


class FaithfulnessResult(BaseModel):
    passed: bool = Field(..., description="答案是否完全被资料支持")
    mismatched_numbers: List[str] = Field(default_factory=list, description="资料中不存在的数值字面量")
    bad_citations: List[int] = Field(default_factory=list, description="资料中不存在的引用页码")
    unsupported_claims: List[str] = Field(default_factory=list, description="资料无法支持的断言")
    reason: str = Field("", max_length=300)

    @model_validator(mode="before")
    def _coerce(cls, data: Any) -> Any:  # noqa: N805
        if not isinstance(data, dict):
            return data
        out = dict(data)
        # 兼容模型常写的 pass / is_faithful / ok 等别名
        if "passed" not in out:
            for key in ("pass", "is_faithful", "faithful", "ok", "result"):
                if key in out:
                    out["passed"] = bool(out[key])
                    break
        for key in ("mismatched_numbers", "bad_citations", "unsupported_claims"):
            if not isinstance(out.get(key), list):
                out[key] = []
        out["bad_citations"] = [int(x) for x in out.get("bad_citations", []) if str(x).strip().lstrip("-").isdigit()]
        return out


class AnswerResult(BaseModel):
    answer: str = Field(..., min_length=1, description="正文，关键句末带 [P页码] 角标")
    citations: List[CitationItem] = Field(default_factory=list, description="引用列表")
    refused: bool = Field(False, description="资料不足或越界时置 true")
    used_chunk_ids: List[str] = Field(default_factory=list, description="实际引用的片段 id")

    @model_validator(mode="before")
    def _coerce(cls, data: Any) -> Any:  # noqa: N805
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if not isinstance(out.get("citations"), list):
            out["citations"] = []
        if not isinstance(out.get("used_chunk_ids"), list):
            out["used_chunk_ids"] = []
        return out


class NewStarNode(BaseModel):
    """模型**提议**的新星图节点。

    注意是提议而非生效：能不能真的进星图由 `retrieval.probe.probe_node` 的可召回性
    验证决定（召回为空、或相关性不过闸门口径就丢弃）。所以这里不做词表约束——
    约束放在验证侧，模型才有空间提出字典里没有、但这份报告里确实存在的科目。
    """

    id: str = Field(..., description="节点 id：英文小写 + 下划线，如 inventory_turnover")
    label: str = Field(..., description="中文显示名，如 存货周转率")
    keywords: List[str] = Field(default_factory=list,
                                description="检索关键词 1-3 个，必须是财报原文会出现的表述")


class UiAnswerResult(BaseModel):
    """星图节点解释：驱动富交互 UI 的最小载荷。

    - explanation：<=350 字，只解释当前点击的单一概念，不延伸分析；
    - citations：页码字符串数组（形如 ["P12"]），必须来自资料真实页码；
    - unlock_next：推荐点亮的 1-2 个关联节点（渐进式披露，必须是星图已定义节点）。
    """

    explanation: str = Field(..., min_length=1, description="当前节点的简明解释，不超过 350 字，关键句末带 [P页码]")
    citations: List[str] = Field(default_factory=list, description="引用来源，形如 ['P12']")
    unlock_next: List[str] = Field(default_factory=list, description="推荐点亮的 1-2 个关联节点 id")
    new_nodes: List[NewStarNode] = Field(default_factory=list,
                                         description="提议的新节点（需通过可召回性验证才会进星图）")
    refused: bool = Field(False, description="资料不足时为 true，explanation 需说明未找到")
    used_chunk_ids: List[str] = Field(default_factory=list, description="实际引用的片段 id")

    @model_validator(mode="before")
    def _coerce(cls, data: Any) -> Any:  # noqa: N805 - pydantic 约定
        if not isinstance(data, dict):
            return data
        out = dict(data)

        # 超长不整条丢弃（那会让用户看到兜底话术），而是硬截断到 350 字。
        # 350 是给"两期对比"留的：一期金额 + 同比变化 + 两处引用约 150-200 字，
        # 100 字时对比数据会被切掉一半，等于白算。
        exp = str(out.get("explanation") or "").strip()
        out["explanation"] = exp[:350] if len(exp) > 350 else exp

        # citations 兼容三种脏写法：["P12"] / [12] / [{"page": 12}]
        cites: List[str] = []
        for c in out.get("citations") or []:
            if isinstance(c, dict):
                p = c.get("page")
            else:
                p = re.sub(r"\D", "", str(c))
            if str(p).strip().isdigit() and int(p) >= 1:
                s = "P%d" % int(p)
                if s not in cites:
                    cites.append(s)
        out["citations"] = cites[:6]

        # unlock_next 去重 + 限 2（LLM 常一次给 3-5 个）
        nxt: List[str] = []
        for n in out.get("unlock_next") or []:
            n = str(n).strip().lower()
            if n and n not in nxt:
                nxt.append(n)
        out["unlock_next"] = nxt[:2]

        # 新节点提议：规范化 id（英文小写下划线）、限 2 个（多了会拖慢探针，
        # 也会让星图一次长出一片未经充分验证的节点）。
        nodes: List[Dict[str, Any]] = []
        for n in out.get("new_nodes") or []:
            if not isinstance(n, dict):
                continue
            nid = re.sub(r"[^a-z0-9_]+", "_", str(n.get("id") or "").strip().lower()).strip("_")
            label = str(n.get("label") or "").strip()
            if not nid or not label:
                continue
            kws: List[str] = []
            for k in (n.get("keywords") or []):
                k = str(k).strip()
                if k and k not in kws:
                    kws.append(k)
            nodes.append({"id": nid, "label": label[:20], "keywords": kws[:3]})
        out["new_nodes"] = nodes[:2]

        if not isinstance(out.get("used_chunk_ids"), list):
            out["used_chunk_ids"] = []
        return out


SCHEMA_REGISTRY: Dict[str, Type[BaseModel]] = {
    "intent": IntentResult,
    "faithfulness": FaithfulnessResult,
    "answer": AnswerResult,
    "ui_answer": UiAnswerResult,
}


def schema_json(model: Type[BaseModel]) -> str:
    """给 Prompt 用的紧凑 JSON Schema（中文描述已在 Field 中）。"""
    return json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2)


def strip_fences(raw: str) -> str:
    """去掉 ```json ... ``` 围栏与首尾空白。"""
    if not raw:
        return ""
    text = raw.strip()
    text = _FENCE.sub("", text).strip()
    return text


def extract_json_object(raw: str) -> str:
    """从任意输出中抠出第一个完整 JSON 对象（容忍前后废话）。"""
    text = strip_fences(raw)
    if not text:
        return ""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:] if depth > 0 else ""


def validate_model(model: Type[BaseModel], raw: str) -> Optional[BaseModel]:
    """宽松解析：抠 JSON -> 校验；失败返回 None（交由调用方修复重试）。"""
    if not raw:
        return None
    cand = extract_json_object(raw) or strip_fences(raw)
    if not cand:
        return None
    try:
        return model.model_validate_json(cand)
    except Exception:
        try:
            return model.model_validate(json.loads(cand))
        except Exception:
            return None


def to_dict(obj: BaseModel) -> Dict[str, Any]:
    return obj.model_dump()
