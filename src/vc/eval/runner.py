"""批量评测执行器：统一指标口径，供 CLI / 扫描脚本 / UI 复用。

指标口径（金融场景最关心的是"别编"）：
- hit：召回片段里是否含期望关键词（召回能力）
- cite：是否产出可溯源引用（可审计性）
- gate：是否通过数值一致性闸门（不编造的第一道防线）
- fabricate：答案里出现、但召回原文中不存在的数字（**幻觉**，越低越好）
- fallback：走了兜底/澄清/拒答（过高=召回不足，过低=防线可能失效）
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from ..graph.nodes.gate import deterministic_check
from ..graph.query_graph import ask

# 金标用例字段：
# q / expect_keywords / expect_pages / expect_refuse / type / answer（可选，参考答案）
EvalCase = Dict[str, Any]

# 兜底答案会截断摘录（"...20…"），截断处的半截数字不是模型编的，属于度量噪声，需剔除
_TRUNCATED_NUM = re.compile(r"\d[\d,\.]*(?=\s*(?:\.\.\.|…))")


def _strip_noise(answer: str) -> str:
    """去掉 [Pxx] 之外的、由摘录截断造成的半截数字。"""
    return _TRUNCATED_NUM.sub(" ", answer or "")


def _percent(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _p95(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(round(0.95 * (len(s) - 1)))
    return round(s[idx], 3)


def evaluate(case: EvalCase, result: Dict[str, Any]) -> Dict[str, Any]:
    """单条用例评分。"""
    answer = result.get("final_answer", "") or ""
    # 兜底链路（低相关/拦截）会把 context 清空，但答案摘录来自 fused/reranked，
    # 因此幻觉判定要覆盖"任何真实召回过的内容"，否则会把兜底摘录误判成幻觉。
    ctx = (result.get("context")
           or result.get("reranked")
           or result.get("fused")
           or [])
    ctx_text = "\n".join((x.get("text", "") for x in ctx))
    citations = result.get("citations") or []
    fallback_reason = result.get("fallback_reason") or ""
    is_fallback = bool(fallback_reason) and fallback_reason != "chitchat"

    # 召回命中：关键词必须出现在召回片段原文里
    hit = True
    for kw in case.get("expect_keywords", []) or []:
        if kw and kw not in ctx_text:
            hit = False

    # 语料外问题（CFQA no-corpus）：没有"标准答案"可比对，
    # 唯一的正确行为是**诚实**——走兜底/澄清/拒答，而不是硬凑一个数字。
    honest = None
    refused = None
    if case.get("mode") == "no-corpus":
        honest = is_fallback or any(k in answer for k in ("未在报告", "未找到", "无法回答", "不提供", "没有相关"))
        hit = bool(honest)
    elif case.get("expect_refuse"):
        # 越界问题必须拒绝
        refused = ("不提供" in answer) or ("推荐" in answer and "不" in answer) or ("无法" in answer)
        hit = bool(refused)
    elif not (case.get("expect_keywords") or []):
        hit = bool(answer.strip())

    # 幻觉：答案里的数字在召回原文中找不到（用与 gate 相同的判定，保证口径一致）
    # 排除两类噪声：a) 摘录截断产生的半截数字；b) 用户问题自带的年份/数字（兜底话术会回述问题）
    from ..text_utils import extract_numbers

    q_nums = {n.replace(",", "").replace(" ", "") for n in extract_numbers(case.get("q", ""))}
    missing_numbers, bad_pages = deterministic_check(_strip_noise(answer), ctx)
    missing_numbers = [n for n in missing_numbers if n not in q_nums]
    fabricated = bool(missing_numbers)

    # 页码命中（CFQA 有标注页码时才有意义）
    expect_pages = [int(p) for p in (case.get("expect_pages") or []) if str(p).strip().isdigit()]
    page_hit = None
    if expect_pages:
        cited = {c.get("page") for c in citations if c.get("page") is not None}
        page_hit = any(p in cited for p in expect_pages)

    # 参考答案数字命中（CFQA 有标准答案时）
    expect_answer = case.get("answer") or ""
    num_match = None
    if expect_answer:
        from ..text_utils import extract_numbers

        want = [n for n in extract_numbers(expect_answer)]
        if want:
            got = "".join(extract_numbers(answer))
            num_match = any(n.replace(",", "") in got for n in want)

    return {
        "q": case.get("q", ""),
        "type": case.get("type", ""),
        "intent": result.get("intent", ""),
        "hit": bool(hit),
        "honest": honest,
        "refused": refused,
        "cite": bool(citations),
        "gate": bool(result.get("gate_pass")),
        "fabricated": fabricated,
        "missing_numbers": missing_numbers[:5],
        "page_hit": page_hit,
        "num_match": num_match,
        "fallback": fallback_reason,
        "is_fallback": is_fallback,
        "retry": int(result.get("retry_count") or 0),
        "degraded": sorted(set(result.get("degraded") or [])),
        "citations": len(citations),
    }


def run_suite(cases: List[EvalCase], *, verbose: bool = False) -> Dict[str, Any]:
    """跑一批用例并汇总指标。"""
    rows: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for case in cases:
        t0 = time.time()
        result = ask(case.get("q", ""))
        latencies.append(round(time.time() - t0, 3))
        row = evaluate(case, result)
        rows.append(row)
        if verbose:
            print("·", row["q"][:40], "->", row["intent"], "| hit=%s | cite=%s | gate=%s | fb=%s"
                  % (row["hit"], row["cite"], row["gate"], row["fallback"] or "-"))

    n = len(rows) or 1
    refused_cases = [r for r in rows if r["refused"] is not None]
    honest_cases = [r for r in rows if r["honest"] is not None]
    page_cases = [r for r in rows if r["page_hit"] is not None]
    num_cases = [r for r in rows if r["num_match"] is not None]

    return {
        "n": len(rows),
        "hit_rate": _percent(sum(1 for r in rows if r["hit"]), n),
        "cite_rate": _percent(sum(1 for r in rows if r["cite"]), n),
        "gate_rate": _percent(sum(1 for r in rows if r["gate"]), n),
        "fabricate_rate": _percent(sum(1 for r in rows if r["fabricated"]), n),
        "fallback_rate": _percent(sum(1 for r in rows if r["is_fallback"]), n),
        # 无对应样本时置 None（而不是 0%），避免报告里出现误导性的 0.0%
        "refuse_rate": _percent(sum(1 for r in refused_cases if r["refused"]), len(refused_cases)) if refused_cases else None,
        "honest_rate": _percent(sum(1 for r in honest_cases if r["honest"]), len(honest_cases)) if honest_cases else None,
        "page_hit_rate": _percent(sum(1 for r in page_cases if r["page_hit"]), len(page_cases)) if page_cases else None,
        "num_match_rate": _percent(sum(1 for r in num_cases if r["num_match"]), len(num_cases)) if num_cases else None,
        "avg_latency": round(sum(latencies) / n, 3),
        "p95_latency": _p95(latencies),
        "rows": rows,
    }


def summary_text(report: Dict[str, Any], title: str = "评测报告") -> str:
    lines = [
        "=========== %s ===========" % title,
        "样本数        : %d" % report["n"],
        "命中率        : %.1f%%" % report["hit_rate"],
        "引用率        : %.1f%%" % report["cite_rate"],
        "数值闸门通过  : %.1f%%" % report["gate_rate"],
        "幻觉率        : %.1f%%" % report["fabricate_rate"],
        "兜底率        : %.1f%%" % report["fallback_rate"],
        "平均耗时      : %.3fs | P95 %.3fs" % (report["avg_latency"], report["p95_latency"]),
    ]
    if report.get("honest_rate") is not None and report.get("n"):
        lines.append("诚实率(语料外) : %.1f%%" % report["honest_rate"])
    if report.get("page_hit_rate") is not None:
        lines.append("页码命中率    : %.1f%%" % report["page_hit_rate"])
    if report.get("num_match_rate") is not None:
        lines.append("答案数值命中  : %.1f%%" % report["num_match_rate"])
    return "\n".join(lines)
