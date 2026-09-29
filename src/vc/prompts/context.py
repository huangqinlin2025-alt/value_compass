"""资料块渲染：把召回片段渲染成带页码的文本（页码是引用合法性的唯一来源）。"""
from __future__ import annotations

from typing import Any, Dict, List


def format_context(chunks: List[Dict[str, Any]], limit: int = 1200) -> str:
    lines: List[str] = []
    for c in chunks or []:
        page = c.get("page", "?")
        text = (c.get("text", "") or "").strip()
        if len(text) > limit:
            text = text[:limit] + "…"
        lines.append("[P%s] %s" % (page, text))
    return "\n\n".join(lines)
