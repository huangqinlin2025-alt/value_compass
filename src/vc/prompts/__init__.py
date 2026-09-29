"""三大脑节点的系统 Prompt 与消息构造器。

约定（金融场景的硬约束，写在这里而不是散落在节点里）：
- 只依据 <资料> 作答，禁止外部知识与自行计算；
- 引用页码只能来自资料中真实出现的页码；
- 越界（买卖建议 / 目标价 / 收益承诺）必须拒答；
- 输出必须是**单个 JSON 对象**，字段与 schema.py 中的 Pydantic 模型一致。

星图（富交互 UI）链路另有一套更短的契约：build_ui_generator_messages -> UiAnswerResult。
"""
from __future__ import annotations

from .context import format_context
from .gate import GATE_SYSTEM, build_gate_messages
from .generator import GENERATOR_SYSTEM, build_generator_messages
from .router import ROUTER_SYSTEM, build_router_messages
from .ui_generator import UI_GENERATOR_SYSTEM, build_ui_generator_messages

__all__ = [
    "ROUTER_SYSTEM",
    "GATE_SYSTEM",
    "GENERATOR_SYSTEM",
    "UI_GENERATOR_SYSTEM",
    "build_router_messages",
    "build_gate_messages",
    "build_generator_messages",
    "build_ui_generator_messages",
    "format_context",
]
