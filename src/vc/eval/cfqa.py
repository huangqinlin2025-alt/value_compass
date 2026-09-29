"""CFQA 适配层（MIT 数据集，仓库只提供问答 JSON，不含年报 PDF）。

两种评测模式：
1. **no-corpus（默认，随时可跑）**：CFQA 的公司/年份与本地知识库不同，
   把问题灌进当前库，考察系统是否"诚实"——应该走兜底/澄清/拒答，**而不是编造数字**。
   这是 faithfulness_gate 的主战场，不需要任何年报 PDF。
2. **with-corpus（可选）**：用户自备对应公司年报 PDF 入库后，再评命中率与页码命中。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .runner import EvalCase

REPO_RAW = "https://raw.githubusercontent.com/ygan/CFQA/main"

CFQA_URLS: Dict[str, str] = {
    "company_test": "%s/dataset/split_by_company/split_by_company_test.json" % REPO_RAW,
    "company_dev": "%s/dataset/split_by_company/split_by_company_dev.json" % REPO_RAW,
    "company_train": "%s/dataset/split_by_company/split_by_company_train.json" % REPO_RAW,
    "year_test": "%s/dataset/split_by_year/split_by_year_test.json" % REPO_RAW,
}

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "data" / "cfqa"


def split_path(split: str, directory: Path = None) -> Path:
    return Path(directory or DEFAULT_DIR) / ("cfqa_%s.json" % split)


def load_cfqa(split: str = "company_test", directory: Path = None) -> List[Dict[str, Any]]:
    """读取已下载的 CFQA 原始记录（字段：股票代码/公司/问题/答案/答案出自/id）。"""
    p = split_path(split, directory)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("data", "items", "records"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return list(data)


def to_cases(records: List[Dict[str, Any]], limit: int = 0, mode: str = "no-corpus") -> List[EvalCase]:
    """CFQA 记录 -> 内部 EvalCase。

    no-corpus 模式下不设 expect_keywords（无法命中），
    评分重点变成"是否诚实拒答/兜底 + 是否编造数字"。
    """
    cases: List[EvalCase] = []
    for rec in records or []:
        q = (rec.get("问题") or rec.get("question") or "").strip()
        if not q:
            continue
        pages = rec.get("答案出自")
        if isinstance(pages, list):
            expect_pages = [p for p in pages if str(p).strip().isdigit()]
        elif str(pages or "").strip().isdigit():
            expect_pages = [int(pages)]
        else:
            expect_pages = []

        case: EvalCase = {
            "q": q,
            "type": "CFQA",
            "source": "cfqa",
            "company": rec.get("公司", ""),
            "stock_code": str(rec.get("股票代码", "") or ""),
            "answer": rec.get("答案", ""),
            "expect_pages": expect_pages if mode == "with-corpus" else [],
            "expect_keywords": [] if mode == "no-corpus" else _keywords_from(rec),
            "mode": mode,
        }
        cases.append(case)
        if limit and len(cases) >= limit:
            break
    return cases


def _keywords_from(rec: Dict[str, Any]) -> List[str]:
    """with-corpus 模式：从问题里挑出可作为"召回证据"的关键词（取最长的前 2 个词）。"""
    q = rec.get("问题", "") or ""
    import re

    terms = [t for t in re.split(r"[，。？、的了多少？\s]+", q) if len(t) >= 2]
    terms.sort(key=len, reverse=True)
    return terms[:2]


def honesty_report(report: Dict[str, Any]) -> str:
    """no-corpus 模式的解读口径：重点看"有没有编"。"""
    return (
        "语料外问题（CFQA，本地库无对应年报）：\n"
        "  兜底/拒答率 %.1f%%（越高越诚实，越界问题就该说没有）\n"
        "  幻觉率     %.1f%%（越低越好；出现即说明 gate 漏了）\n"
        "  引用率     %.1f%%（语料外问题不应伪造引用）"
        % (report["fallback_rate"], report["fabricate_rate"], report["cite_rate"])
    )
