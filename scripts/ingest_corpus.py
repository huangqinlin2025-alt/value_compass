#!/usr/bin/env python3
"""批量入库：把 data/reports/ 下的财报 PDF 逐个跑入库子图。

    # 全量入库（BM25 只在末尾重建一次）
    python3 scripts/ingest_corpus.py --dir data/reports

    # 切换 embedding provider / 维度后必须强制全量重建
    python3 scripts/ingest_corpus.py --dir data/reports --force

    # 只补建 BM25（上一轮批量入库中断时的补救入口）
    python3 scripts/ingest_corpus.py --rebuild-bm25-only

为什么单独一个入口：`ingest()` 每跑一份就重建一次全量 BM25，
而 build_bm25 每次都要加载**此前所有**文档的快照，60 份串行是 O(n²) 的快照 IO。
这里逐文档传 skip_bm25=True，末尾统一 rebuild_bm25() 一次。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.graph.ingest_graph import ingest, rebuild_bm25  # noqa: E402
from src.vc.ingestion.manifest import read_manifest  # noqa: E402
from src.vc.ingestion.verify import verify_index  # noqa: E402


def _brief(result: Dict[str, Any]) -> str:
    plan = result.get("diff_plan") or {}
    stats = result.get("upsert_stats") or {}
    meta = result.get("doc_meta") or {}
    if result.get("skipped"):
        return "skip(%s)" % plan.get("reason", "")
    return "%s | chunks=%s | %s %s | %s" % (
        plan.get("mode", "?"), stats.get("written", 0),
        meta.get("short_name", "") or meta.get("company", "") or "?",
        meta.get("report_period", "?"), meta.get("industry", "") or "-")


def main() -> int:
    ap = argparse.ArgumentParser(description="财报语料批量入库")
    ap.add_argument("--dir", default="data/reports", help="PDF 目录（默认 data/reports）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少份（0=不限）")
    ap.add_argument("--force", action="store_true", help="忽略 manifest 强制全量重建")
    ap.add_argument("--no-defer-bm25", action="store_true",
                    help="逐文档重建 BM25（慢，仅调试用）")
    ap.add_argument("--rebuild-bm25-only", action="store_true", help="只重建 BM25 并校验")
    args = ap.parse_args()

    if args.rebuild_bm25_only:
        print("=== 重建 BM25 ===")
        print(json.dumps(rebuild_bm25(), ensure_ascii=False))
        v = verify_index()
        return 0 if v.get("ok") else 1

    d = Path(args.dir)
    if not d.exists():
        print("目录不存在：%s（先跑 scripts/crawl_reports.py）" % d)
        return 1
    # 按文件名排序：{code}_{period}.pdf，保证可复现的处理顺序
    pdfs: List[Path] = sorted(p for p in d.glob("*.pdf") if not p.name.startswith("."))
    if args.limit:
        pdfs = pdfs[:args.limit]
    if not pdfs:
        print("目录里没有 PDF：%s" % d)
        return 1

    print("待入库 %d 份（%s）" % (len(pdfs), "强制全量" if args.force else "增量"))
    t0 = time.time()
    failed: List[str] = []
    modes: Dict[str, int] = {}

    for i, p in enumerate(pdfs, 1):
        try:
            r = ingest(str(p), force_rebuild=args.force, skip_bm25=not args.no_defer_bm25)
        except Exception as exc:
            failed.append("%s: %s" % (p.name, exc))
            print("[%d/%d] ✘ %s | %s" % (i, len(pdfs), p.name, exc))
            continue
        plan = r.get("diff_plan") or {}
        mode = "skip" if r.get("skipped") else str(plan.get("mode", "?"))
        modes[mode] = modes.get(mode, 0) + 1
        errs = r.get("errors") or []
        flag = "✘" if errs else "✔"
        print("[%d/%d] %s %-22s %s" % (i, len(pdfs), flag, p.name, _brief(r)))
        for e in errs[:3]:
            print("        ! %s | %s | %s" % (e.get("code"), e.get("node"), e.get("message")))

    print("\n=== 汇总 ===")
    print("耗时 %.1fs | 模式分布 %s | 失败 %d" % (time.time() - t0, json.dumps(modes, ensure_ascii=False), len(failed)))
    for f in failed[:10]:
        print("  ✘", f)

    if not args.no_defer_bm25:
        print("\n=== 统一重建 BM25 ===")
        print(json.dumps(rebuild_bm25(), ensure_ascii=False))

    m = read_manifest()
    print("\nmanifest：%d 个文档" % len(m.get("docs", [])))
    by_industry: Dict[str, int] = {}
    for doc in m.get("docs", []):
        by_industry[doc.get("industry", "-")] = by_industry.get(doc.get("industry", "-"), 0) + 1
    print("行业分布：", json.dumps(by_industry, ensure_ascii=False))

    print("\n=== 落盘校验 ===")
    v = verify_index()
    for c in v.get("checks", []):
        print("  %s %-18s 期望=%-10s 实际=%s" % ("✔" if c.get("ok") else "✘", c["name"], c["expect"], c["actual"]))
    print("  结论：%s" % ("一致" if v.get("ok") else "不一致，请检查上面 ✘ 项"))
    return 0 if (not failed and v.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
