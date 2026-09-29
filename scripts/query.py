#!/usr/bin/env python3
"""CLI 问答：python3 scripts/query.py "营业收入是多少" [--trace]

不带 -q 时进入交互式会话（支持多轮追问）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.graph.query_graph import ask  # noqa: E402


def _show(result: dict, show_trace: bool) -> None:
    print("\n" + result.get("final_answer", "(无答案)"))
    if result.get("citations"):
        print("\n引用：")
        for c in result["citations"]:
            print("  · P%s  %s" % (c.get("page"), (c.get("snippet") or "")[:80]))
    if result.get("degraded"):
        print("\n⚠️  已降级：", sorted(set(result["degraded"])))
    if show_trace:
        print("\n--- trace ---")
        print("trace_id :", result.get("trace_id"))
        print("intent   :", result.get("intent"), "(%.2f)" % float(result.get("intent_confidence") or 0))
        print("stats    :", json.dumps(result.get("fusion_stats", {}), ensure_ascii=False))
        for t in result.get("trace", []):
            print("  %-18s %7.1fms  ok=%s %s" % (
                t.get("node"), t.get("latency_ms", 0), t.get("ok"),
                t.get("error_code") or ""))
        if result.get("errors"):
            print("errors   :")
            for e in result["errors"]:
                print("  -", e.get("code"), "|", e.get("node"), "|", e.get("message"))


def main() -> int:
    ap = argparse.ArgumentParser(description="财报问答")
    ap.add_argument("question", nargs="?", default=None)
    ap.add_argument("--trace", action="store_true", help="打印节点耗时与错误")
    args = ap.parse_args()

    if args.question:
        _show(ask(args.question), args.trace)
        return 0

    print("value_compass 财报问答（输入 q 退出）")
    history = []
    while True:
        try:
            q = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("q", "quit", "exit"):
            break
        if not q:
            continue
        result = ask(q, history=history)
        _show(result, args.trace)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": result.get("final_answer", "")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
