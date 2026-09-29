#!/usr/bin/env python3
"""参数扫描：在真实索引上网格调参，挑出最优组合并给出排名表。

    # 检索期参数（无需重建索引，快）
    python3 scripts/sweep.py --stage retrieval --limit 12

    # 切分参数（每组都会重建索引，慢，但能看清 chunk_size 的真实影响）
    python3 scripts/sweep.py --stage chunk --limit 12 --restore-best

评分（越像"可用"越高）：
    score = 0.5*命中率 + 0.2*引用率 + 0.2*闸门通过率 - 1.0*幻觉率 - 0.3*兜底率
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.config import CONFIG  # noqa: E402
from src.vc.eval.runner import run_suite  # noqa: E402

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "goldens" / "qa_set.json"

RETRIEVAL_GRID: Dict[str, List[Any]] = {
    "top_k_bm25": [10, 20],
    "top_k_vector": [10, 20],
    "top_k_final": [3, 5, 8],
    "min_top1_relevance": [0.22, 0.28, 0.34],
}

CHUNK_GRID: Dict[str, List[Any]] = {
    "chunk_size": [800, 1100, 1400],
    "chunk_overlap": [120, 200],
}


def load_cases(limit: int, dataset: str) -> List[Dict[str, Any]]:
    if dataset != "golden":
        p = Path(dataset)
        cases = json.loads(p.read_text(encoding="utf-8"))
    else:
        cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return cases[:limit] if limit else cases


def score_of(report: Dict[str, Any]) -> float:
    return round(
        0.5 * report["hit_rate"]
        + 0.2 * report["cite_rate"]
        + 0.2 * report["gate_rate"]
        - 1.0 * report["fabricate_rate"]
        - 0.3 * report["fallback_rate"],
        3,
    )


def _reset_caches() -> None:
    from src.vc.ingestion.bm25_index import get_bm25
    from src.vc.retrieval.vectorstore import _CACHE as _VS_CACHE

    _VS_CACHE.clear()
    get_bm25(refresh=True)


def run_retrieval_sweep(cases: List[Dict[str, Any]], grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    keys = sorted(grid.keys())
    results = []
    for combo in itertools.product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        for k, v in params.items():
            setattr(CONFIG, k, v)
        report = run_suite(cases)
        results.append({**params, "score": score_of(report), "hit": report["hit_rate"],
                        "cite": report["cite_rate"], "gate": report["gate_rate"],
                        "fabricate": report["fabricate_rate"], "fallback": report["fallback_rate"],
                        "p95": report["p95_latency"]})
        print("  %-58s score=%s" % (json.dumps(params, ensure_ascii=False), results[-1]["score"]))
    return results


def run_chunk_sweep(cases: List[Dict[str, Any]], grid: Dict[str, List[Any]],
                    restore_best: bool) -> List[Dict[str, Any]]:
    from src.vc.graph.ingest_graph import ingest

    keys = sorted(grid.keys())
    results = []
    for combo in itertools.product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        for k, v in params.items():
            setattr(CONFIG, k, v)
        print("重建索引 %s ..." % json.dumps(params, ensure_ascii=False))
        ingest(None, force_rebuild=True)
        _reset_caches()
        report = run_suite(cases)
        results.append({**params, "score": score_of(report), "hit": report["hit_rate"],
                        "cite": report["cite_rate"], "gate": report["gate_rate"],
                        "fabricate": report["fabricate_rate"], "fallback": report["fallback_rate"],
                        "p95": report["p95_latency"]})
        print("  -> score=%s hit=%.1f%%" % (results[-1]["score"], results[-1]["hit"]))

    if restore_best and results:
        best = max(results, key=lambda r: r["score"])
        print("\n用最优参数重建索引：%s" % json.dumps({k: best[k] for k in keys}, ensure_ascii=False))
        for k in keys:
            setattr(CONFIG, k, best[k])
        ingest(None, force_rebuild=True)
        _reset_caches()
    return results


def print_table(results: List[Dict[str, Any]], keys: List[str]) -> None:
    print("\n=========== 参数扫描排名（前 10）===========")
    header = " | ".join([k[:12] for k in keys] + ["score", "hit%", "cite%", "gate%", "hallu%", "fb%"])
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda x: -x["score"])[:10]:
        print(" | ".join([str(r[k]) for k in keys]
                         + [str(r["score"]), "%.1f" % r["hit"], "%.1f" % r["cite"],
                            "%.1f" % r["gate"], "%.1f" % r["fabricate"], "%.1f" % r["fallback"]]))


def main() -> int:
    ap = argparse.ArgumentParser(description="参数网格扫描")
    ap.add_argument("--stage", default="retrieval", choices=["retrieval", "chunk"])
    ap.add_argument("--limit", type=int, default=12, help="每条组合跑多少用例")
    ap.add_argument("--dataset", default="golden", help="golden 或自定义用例文件路径")
    ap.add_argument("--csv", default=None, help="结果写入 CSV")
    ap.add_argument("--restore-best", action="store_true", help="chunk 扫描结束后用最优参数重建索引")
    args = ap.parse_args()

    cases = load_cases(args.limit, args.dataset)
    grid = RETRIEVAL_GRID if args.stage == "retrieval" else CHUNK_GRID
    print("组合数：%d | 用例数：%d" % (len(list(itertools.product(*grid.values()))), len(cases)))

    results = (run_retrieval_sweep(cases, grid) if args.stage == "retrieval"
               else run_chunk_sweep(cases, grid, args.restore_best))
    print_table(results, sorted(grid.keys()))

    if args.csv:
        import csv

        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        print("\nCSV 已写入：%s" % args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
