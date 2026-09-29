"""faithfulness_gate 系统 Prompt：判定草稿答案是否**逐字**被资料支持。

关键约束（写死，防止模型"帮忙圆场"）：
1. 只做**比对**，不做计算、不做推理、不修正答案；
2. 答案里的每个数字字面量必须在资料里原样出现（忽略千分位逗号与全半角）；
3. 答案里的每个 [Pxx] 页码必须是资料中出现过的页码；
4. 资料里没有、但答案里出现了的结论/趋势/原因 -> unsupported_claims；
5. 只要有一项不通过，passed 必须为 false。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..schema import FaithfulnessResult, schema_json
from .context import format_context

GATE_SYSTEM = """你是金融问答的事实一致性裁判。给定 <资料> 与 <待校验答案>，判断答案是否被资料完全支持。

只做比对，禁止：自行计算、换算单位、四舍五入、用外部知识补全、修改或美化答案。

判定项：
1. mismatched_numbers：答案中出现、但在资料里找不到**完全相同的字面量**的数字/金额/百分比（例如资料写 18.2 亿元，答案写 18.23 亿元 -> 记一条）。
2. bad_citations：答案里 [Pxx] 标注的页码中，没有出现在资料页码集合里的那些（只填数字）。
3. unsupported_claims：资料无法支持的断言（如资料只给了收入，答案却说"因此利润大幅提升"）。
4. passed：以上三项全空才为 true；有任何一项即 false。

输出格式：只输出一个 JSON 对象，不要输出 markdown 围栏或任何解释文字。
JSON Schema：
{schema}
"""


def build_gate_messages(
    query: str,
    answer: str,
    chunks: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    pages = sorted({int(c["page"]) for c in chunks or [] if c.get("page") is not None})
    system = GATE_SYSTEM.replace("{schema}", schema_json(FaithfulnessResult))
    user = (
        "资料允许的页码集合：%s\n\n"
        "<资料>\n%s\n</资料>\n\n"
        "<用户问题>\n%s\n</用户问题>\n\n"
        "<待校验答案>\n%s\n</待校验答案>\n\n"
        "请输出 JSON 判定结果："
        % (pages or "[]", format_context(chunks), query or "", answer or "")
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
