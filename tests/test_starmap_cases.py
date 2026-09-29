"""星图契约跑批：20 个典型 Case，只做两条代码级硬校验。

与 `test_e2e_starmap.py` 的分工：
- 那边验证"整车"（主链路流转、Checkpointer 跨轮累加）；
- 这边验证"契约"（真实 LLM 的产出物能否被 Pydantic 吃下、unlock_next 会不会漏出幽灵节点）。

为什么只校验这两条底线：
1. **结构完整性**（`UiAnswerResult.model_validate` 不抛 `ValidationError`）：
   契约一旦加载失败，generator 会静默回落抽取式文本，前端拿到的卡片结构与约定不符，
   而 trace 上每个节点都 `ok=true`——只有把 payload 真正喂给 Pydantic 才暴露得出来。
2. **词典合规性**（`unlock_next` 100% 落在 `STAR_NODES`）：
   模型自造节点 id 会让前端渲染出"点不动的幽灵节点"，该字段还直接驱动渐进式披露路径。

不做语义质量评估（答案对不对、引用准不准交给 `src/vc/eval/`），
否则断言会随模型输出漂移，跑批就失去了"契约守门"的意义。
注意：跑批打印的 `refused` / `degraded` 是**观察量而非断言**——
首轮实跑已据此暴露两个语义层问题（库内标的被闸门误杀、库外标的未拒答），
记录在 `CHANGELOG.md` 的 [0.4.1] 已知问题，待 P1 评测量化后处理。

Case 分组：
- A 组 10 个：正常星图点击，覆盖 `STAR_NODES` 全部 9 个节点 + 1 个重复节点；
- B 组 5 个：跨文档对比，同一/相邻节点 × 不同库内标的；
- C 组 6 个：信息缺失诱导（未选公司 / 库外公司 / 库外期间 / 字典外节点），
  考察降级路径下契约是否依然成立——降级最容易漏出空 payload 与幽灵节点。

除 C21 外每个 case 都下发 `company`：星图点击必须锁定主体，
未锁定会走"请先选择公司"兜底，拿不到真实卡片（详见 test_starmap_scope.py）。

运行（**需要真实 LLM**：`MockLLM.supports_json=False`，走不到 `UiAnswerResult` 契约层）：

    LLM_PROVIDER=openai_compat python3 -m pytest tests/test_starmap_cases.py -v -s

默认（mock）自动 skip，避免每次跑全量测试烧额度、也避免被限流污染结果。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.config import CONFIG  # noqa: E402
from src.vc.graph.query_graph import ask  # noqa: E402
from src.vc.schema import UiAnswerResult  # noqa: E402
from src.vc.ui import STAR_NODES, UI_ACTION_STAR  # noqa: E402

pytestmark = [
    pytest.mark.skipif(
        not CONFIG.manifest_path.exists(),
        reason="需要已构建的检索索引（index/manifest.json），无语料环境跳过",
    ),
    pytest.mark.skipif(
        CONFIG.llm_provider != "openai_compat",
        reason="契约由真实 LLM 产出；MockLLM 不产 UiAnswerResult，"
               "需显式 LLM_PROVIDER=openai_compat 才跑",
    ),
]

# 库内标的（data/reports 真实存在的 A 股六位码），跨文档对比必须命中真实文档
IN_CORPUS = {
    "600519": "贵州茅台", "300750": "宁德时代", "600036": "招商银行",
    "000858": "五粮液", "002594": "比亚迪", "601318": "中国平安",
}

# 星图点击必须锁定公司：不锁定会走"请先选择公司"兜底（见 test_starmap_scope.py），
# 拿不到真实卡片，契约跑批就白跑了。故除 C21 外一律下发 company。
CASES = [
    # ---------- A 组：正常星图点击（10）----------
    {"id": "A01-revenue", "filters": {"node_id": "revenue", "company": "600519"}},
    {"id": "A02-net_profit", "filters": {"node_id": "net_profit", "company": "600519"}},
    {"id": "A03-gross_margin", "filters": {"node_id": "gross_margin", "company": "600519"}},
    {"id": "A04-roe", "filters": {"node_id": "roe", "company": "600519"}},
    {"id": "A05-cashflow", "filters": {"node_id": "cashflow", "company": "600519"}},
    {"id": "A06-asset_liability", "filters": {"node_id": "asset_liability", "company": "600519"}},
    {"id": "A07-business_mix", "filters": {"node_id": "business_mix", "company": "600519"}},
    {"id": "A08-risk", "filters": {"node_id": "risk", "company": "600519"}},
    {"id": "A09-strategy", "filters": {"node_id": "strategy", "company": "600519"}},
    {"id": "A10-revenue@茅台", "filters": {"node_id": "revenue", "company": "600519"}},

    # ---------- B 组：跨文档对比（5）----------
    {"id": "B11-revenue@宁德", "filters": {"node_id": "revenue", "company": "300750"}},
    {"id": "B12-revenue@比亚迪", "filters": {"node_id": "revenue", "company": "002594"}},
    {"id": "B13-net_profit@招行", "filters": {"node_id": "net_profit", "company": "600036"}},
    {"id": "B14-gross_margin@五粮液", "filters": {"node_id": "gross_margin", "company": "000858"}},
    {"id": "B15-roe@平安", "filters": {"node_id": "roe", "company": "601318"}},

    # ---------- C 组：信息缺失诱导（5）----------
    {"id": "C16-库外公司(特斯拉)", "filters": {"node_id": "revenue", "company": "特斯拉"}},
    {"id": "C17-库外公司(苹果)", "filters": {"node_id": "net_profit", "company": "苹果公司"}},
    {"id": "C18-库外期间(1999A)", "filters": {"node_id": "revenue", "company": "600519", "report_period": "1999A"}},
    {"id": "C19-字典外节点(pe_ratio)", "filters": {"node_id": "pe_ratio", "company": "600519"}},
    {"id": "C20-港股(0700)", "filters": {"node_id": "gross_margin", "company": "0700"}},
    # 未选公司：多家公司 + 会话未锁定 -> 不猜主体，回"请先选择公司"卡片
    {"id": "C21-未选公司(裸点)", "filters": {"node_id": "revenue"}},
]


def _brief(exc: ValidationError) -> str:
    """把 ValidationError 压成一行：只报第一个错误，避免刷屏。"""
    errs = exc.errors()
    if not errs:
        return str(exc)[:160]
    e = errs[0]
    loc = ".".join(str(x) for x in (e.get("loc") or []))
    return "%s: %s" % (loc or "<root>", e.get("msg"))


def test_starmap_contract_cases():
    """for 循环跑批：收集全部失败后一次性断言，避免第一个 case 失败就中断整批。"""
    failures = []

    for case in CASES:
        cid = case["id"]
        try:
            # 独立 thread_id：Checkpointer 按 thread 隔离，unlocked_nodes 不会在用例间串味
            result = ask(
                ui_action=UI_ACTION_STAR,
                ui_filters=dict(case["filters"]),
                thread_id="case-%s-%s" % (cid, uuid.uuid4().hex[:8]),
            )
        except Exception as exc:  # 引擎级异常（限流/超时）单独归类，不与契约失败混淆
            failures.append("[引擎] %s 整图调用异常: %r" % (cid, exc))
            continue

        payload = result.get("ui_payload") or {}

        # 底线 1：结构完整性 —— 契约吃不下，等于卡片结构与前端约定不符
        try:
            UiAnswerResult.model_validate(payload)
        except ValidationError as exc:
            failures.append("[结构] %s %s" % (cid, _brief(exc)))

        # 底线 2：词典合规 —— 漏出去就是前端点不动的幽灵节点
        illegal = [n for n in (payload.get("unlock_next") or []) if n not in STAR_NODES]
        if illegal:
            failures.append("[词典] %s unlock_next 含非字典节点 %s" % (cid, illegal))

        print("%-24s unlock=%-26s refused=%s degraded=%s" % (
            cid,
            ",".join(payload.get("unlock_next") or []) or "-",
            payload.get("refused"),
            ",".join(result.get("degraded") or []) or "-",
        ))

    assert not failures, "星图契约跑批失败 %d/%d：\n%s" % (
        len(failures), len(CASES), "\n".join(failures),
    )
