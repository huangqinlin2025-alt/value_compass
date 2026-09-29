#!/usr/bin/env python3
"""评测 CLI：金标 / CFQA / 幻觉探针三套数据集共用一份指标口径。

    python3 scripts/eval.py                          # 本地 22 条金标
    python3 scripts/eval.py --dataset cfqa --limit 50   # CFQA 语料外问题（看是否诚实）
    python3 scripts/eval.py --dataset hallucination  # 注入编造数字，验证 gate 拦截 + retry_shrink

指标：
- 命中率：期望关键词出现在召回片段原文中的比例
- 引用率：产出可溯源引用的比例
- 数值闸门通过：通过 faithfulness_gate 的比例
- 幻觉率：答案中出现、召回原文中不存在的数字（越低越好）
- 兜底率：走了兜底/澄清/拒答的比例（过高说明召回不足，过低说明防线失效）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.eval.cfqa import honesty_report, load_cfqa, to_cases  # noqa: E402
from src.vc.eval.hallucination import run_probes  # noqa: E402
from src.vc.eval.runner import run_suite, summary_text  # noqa: E402

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "goldens" / "qa_set.json"
CFQA_GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "goldens" / "cfqa_no-corpus.json"


def _load_cases(args) -> list:
    if args.dataset == "golden":
        cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    elif args.dataset == "cfqa":
        if args.file:
            cases = json.loads(Path(args.file).read_text(encoding="utf-8"))
        elif CFQA_GOLDEN.exists():
            cases = json.loads(CFQA_GOLDEN.read_text(encoding="utf-8"))
        else:
            cases = to_cases(load_cfqa(), limit=args.limit or 50, mode="no-corpus")
            print("（未找到本地 CFQA 用例，已直接从 data/cfqa 读取；可先跑 scripts/fetch_cfqa.py）")
    else:
        cases = json.loads(Path(args.file or GOLDEN).read_text(encoding="utf-8"))
    return cases[: args.limit] if args.limit else cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="golden", choices=["golden", "cfqa", "hallucination", "custom"])
    ap.add_argument("--file", default=None, help="自定义用例文件（dataset=custom/cfqa 时生效）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.dataset == "hallucination":
        print("=========== 幻觉探针 ===========")
        probes = run_probes()
        ok = True
        for p in probes:
            flag = "✔" if (p["blocked"] and not p["leaked"] and p["retry_count"] >= 1) else "✘"
            print("%s %s | 拦截=%s | retry=%d | 兜底=%s | 错误码=%s"
                  % (flag, p["query"], p["blocked"], p["retry_count"],
                     p["fallback_reason"] or "-", ",".join(p["errors"]) or "-"))
            if p["leaked"] or not p["blocked"] or p["retry_count"] < 1:
                ok = False
                print("   泄漏内容：", p["answer"])
        print("\n结论：%s" % ("防线有效，伪造数字未到达用户" if ok else "防线失效，伪造内容可能被输出"))
        return 0 if ok else 1

    cases = _load_cases(args)
    if not cases:
        print("没有可用用例（CFQA 需先执行 scripts/fetch_cfqa.py）")
        return 1

    report = run_suite(cases, verbose=args.verbose)
    print()
    print(summary_text(report, title="%s 评测报告" % args.dataset))
    if args.dataset == "cfqa":
        print()
        print(honesty_report(report))

    fails = [r for r in report["rows"] if not r["hit"]]
    if fails:
        print("\n未命中样本：")
        for r in fails:
            print("  -", r["q"][:40], "| intent=%s | fallback=%s" % (r["intent"], r["fallback"] or "-"))
    fabricated = [r for r in report["rows"] if r["fabricated"]]
    if fabricated:
        print("\n⚠️ 疑似幻觉（答案数字不在召回原文中）：")
        for r in fabricated:
            print("  -", r["q"][:40], "| 数字:", ",".join(r["missing_numbers"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
