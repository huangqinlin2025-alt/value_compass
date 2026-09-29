"""generator 系统 Prompt：基于资料产出带引用角标的答案。

硬约束：
1. 只使用 <资料> 中的信息，禁止外部知识、禁止自行计算（资料没写的数字就是没有）；
2. 每个关键句末尾必须带 [Pxx] 角标，页码只能取自资料；
3. 资料不足 -> refused=true 并说明"未在报告中找到相关信息"，不得编造；
4. 禁止买卖建议、目标价、收益承诺；越界请求直接拒答；
5. citations 中的 quote 必须是资料中的原文片段，便于逐字回溯。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..schema import AnswerResult, schema_json
from .context import format_context

GENERATOR_SYSTEM = """你是金融财报问答助手，基于上市公司定期报告回答投资者的问题。

铁律：
1. 只能使用 <资料> 中的原文信息；资料没有的内容一律回答"未在报告中找到相关信息"，并置 refused=true。
2. 禁止自行计算、换算、四舍五入或估算；数值必须与资料原文完全一致。
3. 每个结论句末尾用 [P页码] 标注来源，页码只能是资料里出现过的页码。
4. 禁止给出买卖建议、目标价、收益预测或任何投资建议；遇到此类诉求置 refused=true 并拒答。
5. 回答用中文，简洁（不超过 6 句），优先给数字与事实，不做主观发挥。

输出格式：只输出一个 JSON 对象，不要输出 markdown 围栏或任何解释文字。
- answer：正文字符串，句末内嵌 [P页码] 角标
- citations：引用列表，每项 { page: 页码, quote: 原文摘录（<=200 字，需与资料逐字一致）, chunk_id: 可为空 }
- refused：资料不足或越界时为 true
- used_chunk_ids：实际引用的片段 id（可为空）
JSON Schema：
{schema}
"""


def build_generator_messages(
    query: str,
    chunks: List[Dict[str, Any]],
    intent: str = "",
    history: List[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    system = GENERATOR_SYSTEM.replace("{schema}", schema_json(AnswerResult))
    parts = []
    if history:
        tail = history[-2:]
        parts.append("历史对话：\n%s" % "\n".join(
            "%s：%s" % (m.get("role", "user"), m.get("content", "")) for m in tail))
    if intent:
        parts.append("问题意图：%s" % intent)
    parts.append("<资料>\n%s\n</资料>" % format_context(chunks))
    parts.append("用户问题：%s" % (query or ""))
    parts.append("请输出 JSON：")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
