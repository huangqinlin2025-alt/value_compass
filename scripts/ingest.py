#!/usr/bin/env python3
"""CLI 入库：python3 scripts/ingest.py [--path data/sample_report.pdf] [--force]

--force  忽略 manifest，强制全量重建（切换 embedding provider 后必须执行）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.graph.ingest_graph import ingest  # noqa: E402
from src.vc.ingestion.manifest import read_manifest  # noqa: E402
from src.vc.ingestion.verify import verify_index  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="财报 PDF 入库")
    ap.add_argument("--path", default=None, help="PDF 路径，默认 data/sample_report.pdf")
    ap.add_argument("--force", action="store_true", help="强制全量重建")
    args = ap.parse_args()

    t0 = time.time()
    result = ingest(args.path, force_rebuild=args.force)
    cost = time.time() - t0

    print("\n=== 入库结果 ===")
    print("diff_plan :", json.dumps(result.get("diff_plan", {}), ensure_ascii=False))
    print("upsert    :", json.dumps(result.get("upsert_stats", {}), ensure_ascii=False))
    print("bm25      :", json.dumps(result.get("bm25_stats", {}), ensure_ascii=False))
    print("doc_meta  :", json.dumps(result.get("doc_meta", {}), ensure_ascii=False)[:400])
    print("skipped   :", bool(result.get("skipped")))
    print("耗时      : %.2fs" % cost)

    errs = result.get("errors") or []
    if errs:
        print("\n⚠️  错误 %d 条：" % len(errs))
        for e in errs[:10]:
            print("  -", e.get("code"), "|", e.get("node"), "|", e.get("message"))
    if result.get("degraded"):
        print("降级项：", sorted(set(result.get("degraded"))))

    m = read_manifest()
    print("\n当前 manifest：%d 个文档" % len(m.get("docs", [])))
    for d in m.get("docs", []):
        print("  · %s | %s | chunks=%s | v=%s" % (
            d.get("company", "?"), d.get("report_period", "?"),
            d.get("chunk_count", 0), d.get("doc_version", "-")))

    # 落盘一致性：manifest / chroma / bm25 / 快照 四者必须对齐
    print("\n=== 落盘校验 ===")
    v = verify_index()
    for c in v.get("checks", []):
        flag = "✔" if c.get("ok") else "✘"
        print("  %s %-18s 期望=%-10s 实际=%s" % (flag, c["name"], c["expect"], c["actual"]))
    print("  结论：%s" % ("一致" if v.get("ok") else "不一致，请检查上面 ✘ 项"))
    return 0 if (not errs and v.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
