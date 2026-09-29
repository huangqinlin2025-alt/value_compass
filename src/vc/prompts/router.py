"""intent_router 系统 Prompt：把用户问题钉到 9 类意图之一，并给出过滤条件。

设计原则：
1. **合规红线由规则硬覆盖**（本节点拿到 LLM 结果后仍会用规则复核 OOS），防止模型"放行"越界问题；
2. 不确定就 UNCLEAR，宁可走澄清/通用检索，也不要猜错意图；
3. 只输出 JSON，禁止解释性文字。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..schema import INTENT_LITERAL, schema_json
from ..schema import IntentResult

ROUTER_SYSTEM = """你是金融财报问答系统的意图分类器。你的唯一职责是判断用户问题的意图类型，不回答问题本身。

允许输出的意图（只能取其一）：
- METRIC：查询具体财务指标（营业收入、净利润、资产负债率、每股收益等）
- TABLE：查询表格明细/构成/科目列表
- COMPARE：同比、环比、增长、下降、趋势对比
- CALC：需要计算或推导（占比、增长率、换算）
- QUALITATIVE：定性分析（业务、风险、战略、原因）
- SUMMARY：总结、概述、核心要点
- CHITCHAT：打招呼、致谢、问系统能力
- OOS：越界请求（买卖建议、荐股、目标价、行情预测、收益承诺）
- UNCLEAR：信息不足，无法判断

判定规则：
1. 涉及"买入/卖出/能不能买/推荐股票/目标价/涨停/预测股价/明天行情"等投资决策诉求 -> OOS，并置 refused=true。
2. 问题里没有明确对象且不含任何财务语义 -> UNCLEAR，confidence 不高于 0.4。
3. 一句话里同时命中多类时，取**用户最终诉求**那一类；例如"营业收入同比变化多少"是 COMPARE 而非 METRIC。
4. filters 只在问题中明确出现公司名、报告期间（如 2026 半年报）、章节时才填，不要猜。
5. confidence 反映你的确定程度；低于 0.5 时倾向 UNCLEAR。

输出格式：只输出一个 JSON 对象，不要输出 markdown 围栏或任何解释文字。
JSON Schema：
{schema}
"""


def build_router_messages(
    query: str,
    history: Optional[List[Dict[str, str]]] = None,
    rule_hint: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """构造 router 的 messages；rule_hint 是规则打分结果，作为参考但不具约束力。"""
    system = ROUTER_SYSTEM.replace("{schema}", schema_json(IntentResult))
    user_parts = []
    if history:
        tail = history[-3:]
        hist = "\n".join("%s：%s" % (m.get("role", "user"), m.get("content", "")) for m in tail)
        user_parts.append("历史对话：\n%s" % hist)
    if rule_hint:
        user_parts.append("规则初判（仅供参考，可修正）：%s" % rule_hint)
    user_parts.append("用户问题：%s" % (query or ""))
    user_parts.append("请输出 JSON（intent 只能取 %s 之一）：" % "/".join(INTENT_LITERAL))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
