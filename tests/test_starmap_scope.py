"""星图作用域回归：公司维度「未锁定 / 已锁定」的行为边界。

与 `test_starmap_cases.py`（需真实 LLM 的契约跑批）的分工：
这里只断言**规则决定**的行为——作用域解析、硬过滤、兜底话术都不经过 LLM，
因此 mock 环境下也能跑，能进常规回归（不必烧额度）。

守护的是这次修掉的缺陷：前端只传 node_id 时三路召回没有任何硬过滤，
向量路 `where=None` 按语义召回各家公司同一科目的片段（实测点「风险因素」
一次混进隆基/药明/平安/伊利 4 家），卡片只能给出"XX 是指……"的通用定义，
而页码在多份文档间还是歧义 ID（P2 同时属于好几家公司）。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vc.config import CONFIG  # noqa: E402
from src.vc.graph.query_graph import ask  # noqa: E402
from src.vc.knowledge import company_count  # noqa: E402
from src.vc.schema import UiAnswerResult  # noqa: E402
from src.vc.ui import UI_ACTION_STAR  # noqa: E402

pytestmark = pytest.mark.skipif(
    not CONFIG.manifest_path.exists(),
    reason="需要已构建的检索索引（index/manifest.json），无语料环境跳过",
)

# 库内标的（data/reports 真实存在的 A 股六位码）
MAOTAI = "600519"

MULTI_COMPANY = pytest.mark.skipif(
    company_count() < 2,
    reason="单公司语料不存在「未指定公司」的歧义，本组断言不适用",
)


def _thread() -> str:
    """独立 thread_id：Checkpointer 按 thread 隔离，避免用例间串会话作用域。"""
    return "scope-%s" % uuid.uuid4().hex[:8]


@MULTI_COMPANY
def test_bare_click_asks_for_company():
    """多家公司 + 会话未锁定：不猜公司，回"请先选择公司"卡片而非拿某家的资料冒充。"""
    r = ask(ui_action=UI_ACTION_STAR, ui_filters={"node_id": "revenue"}, thread_id=_thread())

    assert "ui:need_company" in (r.get("degraded") or [])
    payload = r.get("ui_payload") or {}
    assert payload.get("refused") is True
    # 没读到任何确定主体的资料，就不该点亮节点（点亮 = 假装有内容）
    assert r.get("unlocked_nodes") == []
    # 话术要指向下一步动作，并把可选项列出来
    assert "请先选择公司" in (payload.get("explanation") or "")
    # 退化卡片同样要能被契约吃下，否则前端拿到结构不符的载荷
    UiAnswerResult.model_validate(payload)
    # 库外哨兵不得污染会话作用域：下一轮问到真公司时应当正常继承
    assert not r.get("session_company")


@MULTI_COMPANY
def test_click_inherits_company_from_natural_query():
    """先问"贵州茅台…"再点节点：主体仍是茅台，不得回到跨公司混引。"""
    tid = _thread()
    ask("贵州茅台的营业收入是多少？", thread_id=tid)
    r = ask(ui_action=UI_ACTION_STAR, ui_filters={"node_id": "net_profit"}, thread_id=tid)

    assert r.get("session_company") == MAOTAI
    ctx = r.get("context") or []
    assert ctx, "锁定公司后应当召得到片段（否则说明过滤把召回打空了）"
    # 硬过滤保证绝大多数片段来自目标公司；BM25 路在过滤后为空时才退回未过滤结果，
    # 因此用主导公司断言而不是"全部"——scope 反映的正是多数派。
    scope = (r.get("ui_payload") or {}).get("scope") or {}
    assert scope.get("stock_code") == MAOTAI, "卡片主体串到了别的公司：%s" % scope


def test_payload_carries_doc_scope():
    """卡片必须带「公司 + 期间」坐标：页码只在二者之下才唯一。

    同一家公司有 6 期报告，P12 在每一期都存在——只有 scope 才能让 "P12" 可定位。
    """
    r = ask(ui_action=UI_ACTION_STAR,
            ui_filters={"node_id": "revenue", "company": MAOTAI},
            thread_id=_thread())

    payload = r.get("ui_payload") or {}
    scope = payload.get("scope") or {}
    assert scope.get("stock_code") == MAOTAI
    # 用户没指定期间时也必须标出主导期，否则引用仍然歧义
    assert scope.get("report_period"), "scope 缺 report_period，页码无法唯一定位"


def test_explicit_period_is_hard_filter():
    """用户显式指定的期间仍然是硬过滤：库外期间必须转兜底，不能放宽成"忽略期间"。"""
    r = ask(ui_action=UI_ACTION_STAR,
            ui_filters={"node_id": "revenue", "company": MAOTAI, "report_period": "1999A"},
            thread_id=_thread())

    assert r.get("unlocked_nodes") == []
    payload = r.get("ui_payload") or {}
    assert payload.get("refused") is True
