"""元数据抽取与富化。

元数据是「第三路召回（元数据过滤）」与「引用溯源」的基础，schema 固定为：
doc_id, doc_version, company, stock_code, report_period, report_type,
page, section_path, table_flag, chunk_id, content_hash, source_path。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..text_utils import normalize_text

# 常见财报章节标题（用于构建 section_path）
SECTION_PATTERNS = [
    (re.compile(r"^第[一二三四五六七八九十]+节\s*(.+)$"), 2),
    (re.compile(r"^[一二三四五六七八九十]+、\s*(.+)$"), 1),
    (re.compile(r"^（[一二三四五六七八九十]+）\s*(.+)$"), 2),
    (re.compile(r"^\d+\.\s*(\S.{0,30})$"), 3),
]
KNOWN_SECTIONS = [
    "重要提示", "公司基本情况", "主要财务数据", "主要会计数据和财务指标",
    "管理层讨论与分析", "经营情况讨论与分析", "重要事项", "股份变动及股东情况",
    "财务报告", "合并财务报表", "资产负债表", "利润表", "现金流量表",
    "董事、监事和高级管理人员", "公司治理", "环境与社会责任", "备查文件",
]

_COMPANY_RE = re.compile(r"([\u4e00-\u9fff（）()A-Za-z0-9]{2,20}?)股份有限公司")
_CODE_RE = re.compile(r"公司代码[：:]\s*(\d{6})")
_SHORT_RE = re.compile(r"公司简称[：:]\s*(\S+)")
_PERIOD_RES = [
    (re.compile(r"(\d{4})\s*年\s*半年度"), "H1"),
    (re.compile(r"(\d{4})\s*年\s*第一季度"), "Q1"),
    (re.compile(r"(\d{4})\s*年\s*第三季度"), "Q3"),
    (re.compile(r"(\d{4})\s*年\s*年度报告"), "A"),
    (re.compile(r"(\d{4})\s*年度"), "A"),
]


def detect_doc_meta(pages_head: List[str]) -> Dict[str, Any]:
    """从封面/首页文本中抽取公司、代码、报告期间。"""
    head = "\n".join(pages_head[:3])
    head = normalize_text(head)
    meta: Dict[str, Any] = {
        "company": "",
        "stock_code": "",
        "report_period": "",
        "report_type": "",
    }
    m = _COMPANY_RE.search(head)
    if m:
        meta["company"] = m.group(1) + "股份有限公司"
    m = _CODE_RE.search(head)
    if m:
        meta["stock_code"] = m.group(1)
    for rx, tag in _PERIOD_RES:
        m = rx.search(head)
        if m:
            meta["report_period"] = "%s%s" % (m.group(1), tag)
            meta["report_type"] = {"H1": "半年报", "Q1": "一季报", "Q3": "三季报", "A": "年报"}.get(tag, "")
            break
    if not meta["company"]:
        m = _SHORT_RE.search(head)
        if m:
            meta["company"] = m.group(1)
    return meta


def is_heading(line: str) -> Optional[str]:
    """判定一行是否为章节标题，返回标题文本。"""
    line = (line or "").strip()
    if not line or len(line) > 40:
        return None
    for rx, level in SECTION_PATTERNS:
        m = rx.match(line)
        if m:
            return m.group(1).strip() if m.groups() else line
    for name in KNOWN_SECTIONS:
        if line.startswith(name) and len(line) <= len(name) + 12:
            return name
    return None


def build_section_path(stack: List[str], heading: Optional[str]) -> str:
    if not heading:
        return " > ".join(stack) if stack else "UNKNOWN"
    # 同名标题不入栈，避免重复
    if stack and stack[-1] == heading:
        return " > ".join(stack)
    stack.append(heading)
    if len(stack) > 4:
        stack.pop(0)
    return " > ".join(stack)
