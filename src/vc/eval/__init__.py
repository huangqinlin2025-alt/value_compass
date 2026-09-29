"""评测层：金标 / CFQA / 幻觉探针 / 参数扫描的共用能力。

放在 src/vc 而不是 tests/ 下，是因为 UI 与 CLI 都要复用同一套指标口径，
避免"评测报告"和"线上表现"各说各话。
"""
from __future__ import annotations

from .cfqa import CFQA_URLS, load_cfqa, to_cases
from .hallucination import probe_fabricated_number, run_probes
from .runner import EvalCase, evaluate, run_suite

__all__ = [
    "EvalCase",
    "evaluate",
    "run_suite",
    "CFQA_URLS",
    "load_cfqa",
    "to_cases",
    "probe_fabricated_number",
    "run_probes",
]
