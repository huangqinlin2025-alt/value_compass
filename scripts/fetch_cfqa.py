#!/usr/bin/env python3
"""下载 CFQA 数据集（MIT）到本地，并生成评测用例。

    python3 scripts/fetch_cfqa.py --split company_test --limit 80

注意：CFQA 仓库**只提供问答 JSON，不含年报 PDF**（需自行从交易所/巨潮下载）。
因此默认按 no-corpus 模式生成用例——这些问题在本地库里没有答案，
考察的是系统会不会"硬编答案"，正是 faithfulness_gate 的主战场。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.eval.cfqa import CFQA_URLS, DEFAULT_DIR, load_cfqa, split_path, to_cases  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tests" / "goldens"


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "value-compass/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 CFQA 数据集")
    ap.add_argument("--split", default="company_test", choices=sorted(CFQA_URLS.keys()))
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="原始数据存放目录")
    ap.add_argument("--limit", type=int, default=80, help="生成多少条评测用例")
    ap.add_argument("--mode", default="no-corpus", choices=["no-corpus", "with-corpus"])
    ap.add_argument("--out", default=None, help="输出用例文件路径")
    args = ap.parse_args()

    dest = split_path(args.split, Path(args.dir))
    print("下载 %s -> %s" % (CFQA_URLS[args.split], dest))
    try:
        download(CFQA_URLS[args.split], dest)
    except Exception as exc:
        print("下载失败：%s" % exc)
        return 1

    records = load_cfqa(args.split, Path(args.dir))
    print("原始记录：%d 条" % len(records))

    out = Path(args.out or (GOLDEN_DIR / ("cfqa_%s.json" % args.mode)))
    cases = to_cases(records, limit=args.limit, mode=args.mode)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已生成用例：%d 条 -> %s" % (len(cases), out))
    if args.mode == "no-corpus":
        print("提示：no-corpus 模式下期望行为是「兜底/拒答且不编造数字」，不是答对。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
