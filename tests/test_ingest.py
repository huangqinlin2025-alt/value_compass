"""入库层单测：表格感知切分、manifest 增量决策与原子写。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.ingestion.cleaner import clean_pages, normalize_icons
from src.vc.ingestion.manifest import plan_diff, read_manifest, write_manifest_atomic
from src.vc.ingestion.splitter import split_pages

DOC_META = {
    "doc_id": "testdoc",
    "company": "测试股份有限公司",
    "stock_code": "600000",
    "report_period": "2026H1",
    "path": "/tmp/test.pdf",
}


def _pages():
    return [
        {"doc_id": "testdoc", "page": 1, "text": "一、公司基本情况\n公司成立于1985年，主营写字楼与商城的出租经营。"},
        {"doc_id": "testdoc", "page": 2, "text": "主要财务数据\n项目 本期 上期 同比\n营业收入 100 90 11%\n净利润 20 18 11%"},
    ]


def test_split_keeps_table_intact():
    chunks = split_pages(_pages(), DOC_META, "v1")
    assert chunks, "应当切出 chunk"
    tables = [c for c in chunks if c["table_flag"]]
    assert tables, "含表头与两行数字的区域应被识别为表格"
    assert any("营业收入" in t["text"] and "净利润" in t["text"] for t in tables), "整表不应被切断"


def test_split_metadata_complete():
    chunks = split_pages(_pages(), DOC_META, "v1")
    for c in chunks:
        assert c["doc_id"] == "testdoc"
        assert c["report_period"] == "2026H1"
        assert c["page"] in (1, 2)
        assert c["section_path"]
        assert c["content_hash"]


def test_clean_normalizes_icon_fonts():
    """符号字体落在 Unicode 私用区（实测 U+F052 等 6 个码位），必须归一成 Unicode 记号。

    私用区字符进不了汉字 unigram/bigram，却会占 BM25 词频，属于纯噪声。
    """
    pages = [{"doc_id": "d", "page": 1,
              "text": "□适用 \uf052不适用\n\uf0b7 第一层次输入值是活跃市场报价。\n未知\uf999符号"}]
    cleaned = clean_pages(pages)[0]["text"]
    assert "\uf052" not in cleaned, "勾选框必须归一"
    assert "☑" in cleaned, "归一后应保留'勾了哪一项'的语义"
    assert "·" in cleaned, "项目符号应归一"
    assert not re.search(r"[\ue000-\uf8ff]", cleaned), "清洗后不应残留任何私用区字符"
    assert normalize_icons("") == ""


def test_plan_diff_full_for_new_doc():
    plan = plan_diff({"docs": []}, "d1", "sha1", ["p1"], "hashing", 384)
    assert plan["mode"] == "full"


def test_plan_diff_skip_when_unchanged():
    m = {"docs": [{"doc_id": "d1", "file_sha256": "sha1", "status": "active",
                   "embedding_provider": "hashing", "embed_dim": 384, "page_hashes": ["p1"]}]}
    plan = plan_diff(m, "d1", "sha1", ["p1"], "hashing", 384)
    assert plan["mode"] == "skip"


def test_plan_diff_full_when_embedding_changed():
    m = {"docs": [{"doc_id": "d1", "file_sha256": "sha1", "status": "active",
                   "embedding_provider": "hashing", "embed_dim": 384, "page_hashes": ["p1"]}]}
    plan = plan_diff(m, "d1", "sha1", ["p1"], "bge", 512)
    assert plan["mode"] == "full" and plan["reason"] == "embedding_changed"


def test_plan_diff_page_level():
    m = {"docs": [{"doc_id": "d1", "file_sha256": "sha1", "status": "active",
                   "embedding_provider": "hashing", "embed_dim": 384,
                   "page_hashes": ["p1", "p2", "p3"]}]}
    plan = plan_diff(m, "d1", "sha2", ["p1", "pX", "p3"], "hashing", 384)
    assert plan["mode"] == "page" and 2 in plan["changed_pages"]


def test_manifest_atomic_write(tmp_path):
    p = tmp_path / "manifest.json"
    write_manifest_atomic({"version": 1, "docs": [{"doc_id": "x"}]}, str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["docs"][0]["doc_id"] == "x"
    assert not (tmp_path / "manifest.json.tmp").exists(), "临时文件必须已被原子替换"
    assert read_manifest(str(p))["docs"][0]["doc_id"] == "x"
    assert read_manifest(str(tmp_path / "missing.json")) == {"version": 1, "updated_at": 0.0, "docs": []}
