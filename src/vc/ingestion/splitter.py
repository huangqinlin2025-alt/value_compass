"""表格感知切分。

财报的核心信息在表格里：表格一旦被按字数切碎，数字就失去行列语义（"12.34" 不知道属于哪一行）。
因此策略是：
1. 先识别表格区（连续行中多数含 ≥2 个数字，或出现"项目/本期/上期/同比/增减"等表头词）；
2. 表格整块成 chunk（超过 max_table_chars 时按行切分，并重复表头行）；
3. 正文按 chunk_size 滑窗切分，overlap 防跨段切断数字；
4. < min_chunk_chars 的残块并入相邻块；
5. 每块文本前置「章节路径｜页码」，提升检索命中与可溯源性。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..config import CONFIG
from ..state import Chunk, Page
from ..text_utils import stable_hash
from .metadata import build_section_path, is_heading

_NUM = re.compile(r"\d+(?:\.\d+)?")
_TABLE_HEAD_WORDS = ("项目", "本期", "上期", "同比", "增减", "期末", "年初", "金额", "占比", "合计", "附注")


def _looks_like_table_line(line: str) -> bool:
    if not line.strip():
        return False
    nums = _NUM.findall(line)
    if len(nums) >= 2:
        return True
    return any(w in line for w in _TABLE_HEAD_WORDS) and len(nums) >= 1


def _split_page_into_blocks(text: str) -> List[Dict[str, Any]]:
    """把一页拆成 [({text, table_flag, heading})]，表格区保持连续整块。"""
    lines = (text or "").splitlines()
    blocks: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_table = None
    headings: List[tuple] = []  # (index_of_block, heading)

    def flush():
        nonlocal buf, buf_table
        if buf:
            blocks.append({"text": "\n".join(buf).strip(), "table_flag": bool(buf_table)})
            buf = []
            buf_table = None

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        h = is_heading(stripped)
        if h:
            flush()
            headings.append((len(blocks), h))
            continue
        is_table = _looks_like_table_line(stripped)
        if buf_table is None:
            buf_table = is_table
        elif is_table != buf_table:
            flush()
            buf_table = is_table
        buf.append(stripped)
    flush()

    # 把标题挂到其后紧邻的 block
    for idx, h in headings:
        for j in range(idx, len(blocks)):
            if blocks[j].get("heading") is None:
                blocks[j]["heading"] = h
                break
    for b in blocks:
        b.setdefault("heading", None)
    return blocks


def _window_split(paragraph: str, size: int, overlap: int) -> List[str]:
    if len(paragraph) <= size:
        return [paragraph]
    out, start = [], 0
    step = max(1, size - overlap)
    while start < len(paragraph):
        out.append(paragraph[start:start + size])
        start += step
    return [x for x in out if x.strip()]


def _split_big_table(text: str, max_chars: int) -> List[str]:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return [text]
    header = lines[0]
    out, buf = [], [header]
    cur = len(header)
    for line in lines[1:]:
        if cur + len(line) + 1 > max_chars and len(buf) > 1:
            out.append("\n".join(buf))
            buf, cur = [header], len(header)
        buf.append(line)
        cur += len(line) + 1
    if len(buf) > 1:
        out.append("\n".join(buf))
    return out or [text]


def split_pages(
    pages: List[Page],
    doc_meta: Dict[str, Any],
    doc_version: str,
    *,
    chunk_size: int = None,
    chunk_overlap: int = None,
    min_chunk_chars: int = None,
) -> List[Chunk]:
    chunk_size = chunk_size or CONFIG.chunk_size
    chunk_overlap = chunk_overlap or CONFIG.chunk_overlap
    min_chunk_chars = min_chunk_chars or CONFIG.min_chunk_chars

    doc_id = doc_meta.get("doc_id", "")
    stack: List[str] = []
    chunks: List[Chunk] = []
    seq = 0

    for pg in pages:
        page_no = int(pg.get("page", 0))
        blocks = _split_page_into_blocks(pg.get("text", ""))
        for b in blocks:
            heading = b.get("heading")
            if heading:
                build_section_path(stack, heading)
            section = " > ".join(stack) if stack else "UNKNOWN"
            texts: List[str]
            if b["table_flag"]:
                texts = _split_big_table(b["text"], max(chunk_size * 2, 1600))
            else:
                texts = _window_split(b["text"], chunk_size, chunk_overlap)

            for t in texts:
                t = t.strip()
                if not t:
                    continue
                prefix = "[P%s｜%s]" % (page_no, section)
                body = "%s\n%s" % (prefix, t)
                seq += 1
                chunks.append(
                    Chunk(
                        chunk_id="%s-%05d" % (doc_id, seq),
                        doc_id=doc_id,
                        doc_version=doc_version,
                        company=doc_meta.get("company", ""),
                        short_name=doc_meta.get("short_name", ""),
                        stock_code=doc_meta.get("stock_code", ""),
                        report_period=doc_meta.get("report_period", ""),
                        report_type=doc_meta.get("report_type", ""),
                        industry=doc_meta.get("industry", ""),
                        page=page_no,
                        section_path=section,
                        table_flag=bool(b["table_flag"]),
                        text=body,
                        content_hash=stable_hash(body),
                        source_path=doc_meta.get("path", ""),
                        source_url=doc_meta.get("source_url", ""),
                        ingest_time=doc_meta.get("ingest_time", ""),
                        embedding_provider=doc_meta.get("embedding_provider", ""),
                        embed_dim=int(doc_meta.get("embed_dim", 0) or 0),
                    )
                )

    return _merge_small_chunks(chunks, min_chunk_chars)


def _merge_small_chunks(chunks: List[Chunk], min_chars: int) -> List[Chunk]:
    """把过短残块并入相邻块，避免碎片化稀释召回。"""
    if len(chunks) <= 1:
        return chunks
    out: List[Chunk] = []
    for c in chunks:
        if out and len(c["text"]) < min_chars and c["page"] == out[-1]["page"]:
            prev = out[-1]
            merged = dict(prev)
            merged["text"] = prev["text"] + "\n" + c["text"]
            merged["content_hash"] = stable_hash(merged["text"])
            merged["table_flag"] = bool(prev.get("table_flag") or c.get("table_flag"))
            out[-1] = merged
        else:
            out.append(c)
    return out
