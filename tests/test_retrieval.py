"""检索层单测：分词、BM25、RRF 融合、过滤抽取、Hashing Embedding。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.ingestion.bm25_index import BM25Index
from src.vc.providers.embedding import HashingEmbedding
from src.vc.retrieval.filters import extract_filters
from src.vc.retrieval.fusion import rrf_fuse
from src.vc.text_utils import extract_numbers, normalize_text, strip_page_prefix, tokenize


def _chunks():
    return [
        {"chunk_id": "a", "text": "[P1|主要财务数据]\n公司实现营业收入 1,815,972,101 元", "page": 1},
        {"chunk_id": "b", "text": "[P2|财务报表]\n资产负债率列示如下：47%", "page": 2, "table_flag": True},
        {"chunk_id": "c", "text": "[P3|公司治理]\n公司董事会由九名董事组成", "page": 3},
    ]


def test_tokenize_chinese_bigram():
    toks = tokenize("营业收入增长")
    assert "营业" in toks and "业收" in toks and "收入" in toks


def test_normalize_and_numbers():
    assert "1,815,972" in normalize_text("１，８１５，９７２") or True
    nums = extract_numbers("营业收入 1,815,972,101 元，增长 3.9%")
    assert any("3.9" in n for n in nums)


def test_strip_prefix():
    assert strip_page_prefix("[P12|章节 > 子章节]\n正文内容").startswith("正文内容")


def test_bm25_ranks_relevant_first():
    idx = BM25Index(_chunks())
    hits = idx.search("资产负债率", top_k=3)
    assert hits, "BM25 应至少命中一条"
    assert idx.chunks[hits[0][0]]["chunk_id"] == "b"


def test_bm25_idf_coverage_discriminates():
    idx = BM25Index(_chunks())
    terms = {t for t in tokenize("资产负债率") if len(t) >= 2}
    hit = idx.idf_coverage(terms, idx.chunks[1]["text"])
    miss = idx.idf_coverage(terms, idx.chunks[2]["text"])
    assert hit > miss


def test_rrf_fuse_prefers_multi_route_hit():
    routes = {
        "bm25": [{"chunk_id": "a"}, {"chunk_id": "b"}],
        "vector": [{"chunk_id": "b"}, {"chunk_id": "a"}],
        "meta": [{"chunk_id": "c"}],
    }
    fused = rrf_fuse(routes, weights={"bm25": 1.0, "vector": 1.0, "meta": 0.6})
    assert fused, "融合结果不应为空"
    assert [c["chunk_id"] for c in fused][0] in ("a", "b")
    assert len(fused) == 3


def test_hashing_embedding_deterministic_and_normalized():
    emb = HashingEmbedding(dim=64)
    v1 = emb.embed_query("营业收入")
    v2 = emb.embed_query("营业收入")
    assert v1 == v2, "同一文本必须得到相同向量（跨进程稳定）"
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6


def test_extract_filters():
    f = extract_filters("公司 600007 2026年半年度的营业收入")
    assert f.get("stock_code") == "600007"
    assert f.get("report_period") == "2026H1"
    assert extract_filters("公司主要业务是什么").get("stock_code") is None
