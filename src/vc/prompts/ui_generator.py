"""星图节点解释的系统 Prompt：强制 JSON、强制短文本、强制可溯源。

与文本链路（generator.py）的差别：
- 只解释「一个节点」，不做延展分析与投资评价；
- explanation 硬限 100 字，服务于卡片的渐进式披露；
- unlock_next 只能从候选节点里挑，禁止自造 id；确实需要新科目时走 new_nodes 提议，
  由系统做可召回性验证后才放行（验证口径见 `docs/design/nodes.md` 2.5）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..schema import UiAnswerResult, schema_json
from .context import format_context

UI_GENERATOR_SYSTEM = """你是金融财报「知识星图」的节点讲解助手。

铁律：
1. 只解释【当前节点】这一个概念，不延伸分析、不做投资评价、不给买卖建议。
2. 只使用 <资料> 中的原文信息；资料没有就置 refused=true 并说明"未在报告中找到相关信息"，禁止编造。
3. explanation 不超过 350 字，关键句末用 [P页码] 标注来源，页码只能取自资料。
   单期约 80-120 字即可；两期对比时给出"两期数值 + 同比变化"，控制在 200 字内。
4. 必须给出【讲解主体】的**具体数值或具体要点**（金额、比率、同比变化、主要构成项）；
   禁止只写"XX是指……"这类通用定义——定义最多一句，随后必须落到该主体的实际数据。
   只有资料里确实没有任何该主体的数据时，才允许置 refused=true。
5. unlock_next 只能从【候选节点】中选择 1-2 个，禁止在这里自造 id；【已点亮】中的不要再推荐。
6. 只有【候选节点】里确实没有、而资料中又明显存在另一个值得单独查看的科目时，
   才可用 new_nodes 提议 1-2 个新节点：id 用英文小写下划线、label 用中文名，
   并给 1-3 个**财报原文会出现的**关键词（如 ["存货周转率", "存货周转天数"]）。
   系统会拿这些关键词去报告里检索验证，验证不通过的节点会被直接丢弃，所以：
   - 不要提议报告里没有的科目（如"市盈率""市净率"这类定期报告不披露的估值指标）；
   - 不要把已有节点换个名字重新提议；
   - 拿不准就不提议，宁可少一个节点，也不要让用户点进空卡片。
7. 禁止输出 markdown 围栏或任何解释文字，只输出一个 JSON 对象。

JSON Schema：
{schema}
"""


def build_ui_generator_messages(
    node_label: str,
    chunks: List[Dict[str, Any]],
    candidates: List[str] = None,
    unlocked: List[str] = None,
    filters: Dict[str, Any] = None,
    history: List[Dict[str, str]] = None,
    scope: str = "",
) -> List[Dict[str, str]]:
    system = UI_GENERATOR_SYSTEM.replace("{schema}", schema_json(UiAnswerResult))
    parts: List[str] = []
    # 主体（公司 + 期间）必须点名：不点名时模型只会复述概念定义，
    # 卡片里全是"XX是指……"的教科书句子，对该公司一无所知。
    if scope:
        parts.append("讲解主体：%s" % scope)
    if filters:
        parts.append("检索约束：%s" % filters)
    if history:
        parts.append("历史对话：\n%s" % "\n".join(
            "%s：%s" % (m.get("role", "user"), m.get("content", "")) for m in history[-2:]))
    parts.append("当前节点：%s" % (node_label or ""))
    parts.append("已点亮：%s" % ("、".join(unlocked or []) or "无"))
    parts.append("候选节点：%s" % ("、".join(candidates or []) or "无"))
    parts.append("<资料>\n%s\n</资料>" % format_context(chunks))
    parts.append("请输出 JSON：")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
