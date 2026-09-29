"""财报语料层单测：sidecar 元数据、行业过滤、爬虫标题筛选与安全白名单。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.vc.ingestion.sidecar import load_sidecar, merge_doc_meta, sidecar_path
from src.vc.ingestion.splitter import split_pages
from src.vc.retrieval.filters import (
    _match_industry,
    apply_filters_to_chunks,
    chunk_matches,
    extract_filters,
    to_chroma_where,
)
from crawl_reports import _assert_host, wanted_report  # noqa: E402

DETECTED = {
    "company": "贵州茅台酒股份有限公司",
    "stock_code": "600519",
    "report_period": "2024A",
    "report_type": "年报",
}


# --------------------------------------------------------------------- sidecar


def test_sidecar_path_is_sibling_json():
    assert sidecar_path("data/reports/600519_2024A.pdf").name == "600519_2024A.meta.json"


def test_load_sidecar_missing_or_broken(tmp_path):
    assert load_sidecar(str(tmp_path / "nope.pdf")) == {}
    p = tmp_path / "600519_2024A.pdf"
    p.write_text("x")
    (tmp_path / "600519_2024A.meta.json").write_text("{broken", encoding="utf-8")
    assert load_sidecar(str(p)) == {}


def test_load_sidecar_strips_html_and_unknown_fields(tmp_path):
    p = tmp_path / "600519_2024A.pdf"
    p.write_text("x")
    (tmp_path / "600519_2024A.meta.json").write_text(
        json.dumps({"company": "贵州茅台酒股份有限公司",
                    "announcement_title": "贵州茅台<em>2024年年度报告</em>",
                    "evil": "<script>alert(1)</script>"}, ensure_ascii=False),
        encoding="utf-8")
    s = load_sidecar(str(p))
    assert s["announcement_title"] == "贵州茅台2024年年度报告", "公告标题里的高亮标签要被清掉"
    assert "evil" not in s, "不在白名单内的字段不得进入 doc_meta"


def test_merge_sidecar_wins_over_pdf_head():
    sc = {"company": "宁德时代新能源科技股份有限公司", "short_name": "宁德时代",
          "stock_code": "300750", "report_period": "2025H1", "report_type": "半年报",
          "industry": "新能源", "source_url": "http://static.cninfo.com.cn/x.PDF"}
    out = merge_doc_meta(sc, DETECTED)
    assert out["stock_code"] == "300750", "sidecar 必须覆盖 PDF 首页正则的结果"
    assert out["industry"] == "新能源"
    assert out["meta_source"] == "sidecar"
    assert out["sidecar_missing"] is False


def test_merge_falls_back_and_keeps_industry_empty():
    out = merge_doc_meta({}, DETECTED)
    assert out["stock_code"] == "600519", "无 sidecar 时应回落到 PDF 首页正则"
    assert out["industry"] == "", "行业无法从 PDF 正文猜出，必须留空而不是瞎填"
    assert out["meta_source"] == "pdf_head"
    assert out["sidecar_missing"] is True


def test_merge_partial_sidecar_keeps_detected_rest():
    out = merge_doc_meta({"short_name": "贵州茅台"}, DETECTED)
    assert out["short_name"] == "贵州茅台" and out["stock_code"] == "600519"


# ------------------------------------------------------- 新字段贯穿到 chunk


def test_splitter_propagates_industry_and_source():
    meta = {"doc_id": "d1", "company": "比亚迪股份有限公司", "short_name": "比亚迪",
            "stock_code": "002594", "report_period": "2025A", "report_type": "年报",
            "industry": "汽车", "source_url": "http://static.cninfo.com.cn/a.PDF",
            "path": "data/reports/002594_2025A.pdf"}
    pages = [{"doc_id": "d1", "page": 1, "text": "主要财务数据\n项目 本期 上期\n营业收入 100 90 10%"}]
    chunks = split_pages(pages, meta, "v1")
    assert chunks
    for c in chunks:
        assert c["industry"] == "汽车"
        assert c["short_name"] == "比亚迪"
        assert c["source_url"].startswith("http://static.cninfo.com.cn/")


# ----------------------------------------------------------------- 行业过滤


def test_industry_matched_only_when_known():
    assert _match_industry("白酒公司毛利率对比", ["白酒", "新能源"]) == "白酒"
    assert _match_industry("电池企业谁更强", ["白酒", "新能源"]) == "新能源"
    assert _match_industry("白酒", []) == "白酒", "known 为空时按全量词表匹配"
    assert _match_industry("营业收入是多少", ["白酒"]) == "", "没提行业就不过滤"


def test_extract_filters_company_and_industry():
    known = {"docs": [{"company": "宜宾五粮液股份有限公司", "short_name": "五粮液",
                       "stock_code": "000858", "industry": "白酒"}],
             "industries": ["白酒", "银行"]}
    assert extract_filters("五粮液2024年净利润", known).get("stock_code") == "000858"
    assert extract_filters("白酒行业毛利率对比", known).get("industry") == "白酒"
    assert extract_filters("本期营业收入是多少", known) == {}, "无公司无期间无行业 -> 空过滤"


def test_period_extract_never_yields_none_year():
    known = {"docs": [{"company": "贵州茅台酒股份有限公司", "short_name": "贵州茅台",
                       "stock_code": "600519", "industry": "白酒"}], "industries": ["白酒"]}
    assert extract_filters("贵州茅台2024年年度报告的营业收入", known)["report_period"] == "2024A"
    assert extract_filters("2025年半年度报告净利润", known)["report_period"] == "2025H1"
    assert extract_filters("2024年一季报营收", known)["report_period"] == "2024Q1"
    # 只说"年度报告"没给年份：不得拼出 NoneA（库里永远匹配不到 = 静默禁掉过滤）
    assert "report_period" not in extract_filters("年度报告里的营业收入是多少", known)


def test_chroma_where_wraps_multi_conditions_with_and():
    assert to_chroma_where({"stock_code": "600519"}) == {"stock_code": "600519"}
    assert to_chroma_where({}) == {}
    assert to_chroma_where({"stock_code": "600519", "report_period": "2024A"}) == {
        "$and": [{"stock_code": "600519"}, {"report_period": "2024A"}]}, \
        "多条件必须显式 $and：本机 Chroma 不接受隐式多键 AND"


def test_chunk_matches_helper():
    f = {"stock_code": "600519", "industry": "白酒"}
    assert chunk_matches({"stock_code": "600519", "industry": "白酒"}, f)
    assert not chunk_matches({"stock_code": "000858", "industry": "白酒"}, f)
    assert chunk_matches({"stock_code": "600519", "industry": "银行"}, f) is False
    assert chunk_matches({}, {}) is True, "空过滤不得误杀"


def test_bm25_search_where_is_filter_first():
    """公司名常出现在页眉被清洗掉，先取 BM25 top-N 再过滤会整路为空。"""
    from src.vc.ingestion.bm25_index import BM25Index

    def mk(cid, code, tail):
        return {"chunk_id": cid, "stock_code": code,
                "text": "本期 营业收入 同比 增长 %s" % tail}

    chunks = [mk("a1", "300750", "动力电池 出货量"), mk("a2", "300750", "储能 业务"),
              mk("b1", "601012", "光伏组件 出货量"), mk("b2", "601012", "硅片 业务"),
              mk("c1", "600519", "白酒 销量"), mk("c2", "600519", "系列酒 结构")]
    idx = BM25Index(chunks)
    q = "营业收入 同比 增长"
    assert len(idx.search(q, top_k=6)) > 1, "前提：不做过滤时多家公司都会命中"

    hits = idx.search_where(q, lambda c: c.get("stock_code") == "300750", top_k=10)
    assert hits, "过滤优先必须能在范围内找到命中"
    assert all(chunks[i]["stock_code"] == "300750" for i, _ in hits)
    assert idx.search_where(q, lambda c: c.get("stock_code") == "999999") == []


def test_apply_filters_by_industry_and_where():
    chunks = [{"industry": "白酒", "stock_code": "600519"},
              {"industry": "银行", "stock_code": "600036"},
              {}]
    kept = apply_filters_to_chunks(chunks, {"industry": "白酒"})
    assert len(kept) == 1 and kept[0]["stock_code"] == "600519"
    assert len(apply_filters_to_chunks(chunks, {})) == 3, "空过滤不得误杀"
    assert to_chroma_where({"industry": "白酒", "table_flag": True}) == {
        "$and": [{"industry": "白酒"}, {"table_flag": True}]}


# ------------------------------------------------------------- 爬虫标题筛选


def test_wanted_report_filters_variants():
    years = [2023, 2024, 2025]
    assert wanted_report("贵州茅台2024年年度报告", "annual", years) == 2024
    assert wanted_report("2025年度报告", "annual", years) == 2025
    assert wanted_report("贵州茅台2024年年度报告摘要", "annual", years) is None
    assert wanted_report("贵州茅台2024年年度报告（英文版）", "annual", years) is None
    assert wanted_report("贵州茅2024年半年度报告", "annual", years) is None, "半年报不得被当年报抓"
    assert wanted_report("2025年半年度报告（更新前）", "semi", years) is None
    assert wanted_report("2025年半年度报告（更新后）", "semi", years) == 2025
    assert wanted_report("中国平安2025年中期报告", "semi", years) == 2025, "AH 股半年报叫中期报告"
    assert wanted_report("2022年年度报告", "annual", years) is None, "范围外的年份不要"


def test_host_whitelist_blocks_other_domains():
    for ok in ("http://www.cninfo.com.cn/new/hisAnnouncement/query",
               "http://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF"):
        _assert_host(ok)
    for bad in ("http://10.0.0.1/internal", "http://evil.example.com/x.PDF",
                "http://cninfo.com.cn.evil.com/x"):
        try:
            _assert_host(bad)
        except ValueError:
            continue
        raise AssertionError("非白名单域名必须被拒绝：%s" % bad)
