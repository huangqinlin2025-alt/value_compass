"""网页侧元数据 sidecar（`<pdf>.meta.json`）的读取与合并。

为什么需要它：
`detect_doc_meta` 只能从 PDF 首页正则猜公司/期间，而财报封面格式千差万别
（有的叫「中期报告」、有的把代码印在页眉、有的首页就是三张图），抽不到就退化成空值，
`meta_recall` 便无法精确限定「某家公司某年的财报」。
爬虫阶段网页上本来就有权威的简称/代码/报告期/行业，落盘到 sidecar 后这里接管：

合并优先级 —— **sidecar 非空即覆盖，缺失才回落到 PDF 首页正则**，
并把来源写成 `meta_source`，便于排查"元数据到底从哪来"。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

# 允许从 sidecar 进入 doc_meta 的字段白名单（防下载端塞入任意内容）
SIDECAR_FIELDS: List[str] = [
    "company", "short_name", "stock_code", "exchange",
    "report_period", "report_period_cn", "report_type",
    "industry", "sw_level1", "sw_level2",
    "announcement_title", "publish_date", "source_url",
]

# 与 PDF 首页正则共同维护的字段：sidecar 优先
MERGE_FIELDS: List[str] = [
    "company", "short_name", "stock_code", "report_period", "report_type", "industry",
]

_TAG_RE = re.compile(r"<[^>]+>")


def sidecar_path(pdf_path: str) -> Path:
    """`600519_2024A.pdf` -> `600519_2024A.meta.json`"""
    p = Path(pdf_path)
    return p.with_name(p.stem + ".meta.json")


def _clean(v: Any) -> str:
    """去掉公告标题里的高亮标签（巨潮命中词会套 <em>），压平空白。"""
    s = "" if v is None else str(v)
    s = _TAG_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def load_sidecar(pdf_path: str) -> Dict[str, Any]:
    """读取 sidecar；不存在或损坏时返回 {}（不抛异常，入库继续走正则兜底）。"""
    if not pdf_path:
        return {}
    p = sidecar_path(pdf_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for k in SIDECAR_FIELDS:
        v = data.get(k)
        if v in (None, "", [], {}):
            continue
        out[k] = _clean(v) if isinstance(v, str) else v
    return out


def merge_doc_meta(sidecar: Dict[str, Any], detected: Dict[str, Any]) -> Dict[str, Any]:
    """sidecar 优先 + PDF 首页正则兜底，输出 doc_meta 的元数据片段。"""
    sidecar = sidecar or {}
    detected = detected or {}
    out: Dict[str, Any] = {}

    hit_sidecar = False
    for k in MERGE_FIELDS:
        sv = str(sidecar.get(k) or "").strip()
        if sv:
            out[k] = sv
            hit_sidecar = True
        else:
            out[k] = str(detected.get(k) or "").strip()

    # sidecar 独有字段（行业细分 / 公告标题 / 源 URL）直接透传
    for k in SIDECAR_FIELDS:
        if k in MERGE_FIELDS or k in out:
            continue
        v = sidecar.get(k)
        if v not in (None, "", [], {}):
            out[k] = v

    out["meta_source"] = "sidecar" if hit_sidecar else "pdf_head"
    out["sidecar_missing"] = not hit_sidecar
    return out
