"""PDF 加载：复用 test_loader.py 中已验证的 PyPDFLoader 用法。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ..errors import ErrorCode, VCException
from ..state import Page
from ..text_utils import stable_hash


def load_pdf(path: str) -> List[Page]:
    p = Path(path)
    if not p.exists():
        raise VCException(ErrorCode.E_LOAD, "文件不存在: %s" % path)
    if p.suffix.lower() not in (".pdf",):
        raise VCException(ErrorCode.E_LOAD, "暂不支持的文件类型: %s" % p.suffix)

    try:
        from langchain_community.document_loaders import PyPDFLoader
    except Exception as exc:  # pragma: no cover
        raise VCException(ErrorCode.E_LOAD, "PyPDFLoader 不可用: %s" % exc)

    try:
        docs = PyPDFLoader(str(p)).load()
    except Exception as exc:
        raise VCException(ErrorCode.E_LOAD, "PDF 解析失败（可能加密或损坏）: %s" % exc)

    doc_id = stable_hash(str(p.resolve()))[:16]
    pages: List[Page] = []
    for i, d in enumerate(docs):
        text = d.page_content or ""
        pages.append(
            Page(
                doc_id=doc_id,
                page=int(d.metadata.get("page", i)) + 1,  # PyPDFLoader 从 0 开始
                text=text,
                page_hash=stable_hash(text),
            )
        )
    if not pages:
        raise VCException(ErrorCode.E_LOAD, "PDF 未解析出任何页面: %s" % path)
    return pages


def file_fingerprint(path: str) -> Dict[str, Any]:
    """文件级指纹：用于判断是否需要重新入库。"""
    p = Path(path)
    stat = p.stat()
    return {
        "path": str(p),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "file_sha256": _sha256_file(p),
    }


def _sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
