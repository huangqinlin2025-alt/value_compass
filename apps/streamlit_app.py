#!/usr/bin/env python3
"""Streamlit 交互层：streamlit run apps/streamlit_app.py

把 LangGraph 的执行过程"摊开"给用户看：
- 知识星图：分组节点可点击点亮，已点亮 / 推荐 / 新发现三种状态一眼可见；
- 节点卡片：讲解正文 + 溯源角标，对比模式下按期间给出各自的页码；
- 答案区：`[Pxx]` 角标渲染成金色可溯源标记，下方给出原文摘录；
- 证据抽屉：召回片段的分数、命中路由、重排分；
- Trace 时间线：每个节点耗时横条 + 成败/错误码。

只做展示与参数调节，不复制任何业务逻辑（全部走 ask()）。

会话隔离：thread_id 在 session_state 里只生成一次。Checkpointer 按 thread_id 分会话，
共用默认值会让不同浏览器标签的星图进度串在同一条时间线上。
"""
from __future__ import annotations

import html
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from src.vc.config import CONFIG  # noqa: E402
from src.vc.graph.query_graph import ask, starmap_state  # noqa: E402
from src.vc.ingestion.verify import verify_index  # noqa: E402
from src.vc.knowledge import SUGGESTIONS, all_docs, known_meta  # noqa: E402
from src.vc.text_utils import clean_display  # noqa: E402
from src.vc.ui import (  # noqa: E402
    STAR_GROUPS,
    STAR_NODES,
    UI_ACTION_LINK,
    UI_ACTION_RESET,
    UI_ACTION_RESET_ALL,
    UI_ACTION_STAR,
    VIA_TEXT_LINK,
    label_for_node,
    period_cn,
)

st.set_page_config(page_title="value_compass · 财报问答", page_icon="🧭", layout="wide")

CSS = """
<style>
  :root{
    --bg:#0E1420; --panel:#161E2D; --panel2:#1E2739; --line:#26314A;
    --text:#E8EDF5; --muted:#9AA8BF; --blue:#2E6BE6; --blue2:#4C8DFF;
    --gold:#E8B23A; --green:#3FBF7F; --red:#E5544B;
  }
  .stApp{background:radial-gradient(1200px 600px at 15% -10%, #16233c 0%, var(--bg) 55%) fixed;}
  .vc-wrap{background:linear-gradient(180deg, rgba(22,30,45,.92), rgba(14,20,32,.92));
    border:1px solid var(--line); border-radius:14px; padding:14px 16px;
    box-shadow:0 10px 30px rgba(0,0,0,.35); backdrop-filter:blur(8px);}
  .vc-top{display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:10px;}
  .vc-title{font-size:26px; font-weight:600; letter-spacing:.2px; margin:0;}
  .vc-sub{color:var(--muted); font-size:13px;}
  .vc-chip{display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px;
    font-size:12px; border:1px solid var(--line); background:rgba(46,107,230,.12); color:var(--text);}
  .vc-chip.gold{border-color:rgba(232,178,58,.5); background:rgba(232,178,58,.12); color:#F3D68B;}
  .vc-chip.green{border-color:rgba(63,191,127,.5); background:rgba(63,191,127,.12); color:#9BE7BE;}
  .vc-chip.red{border-color:rgba(229,84,75,.5); background:rgba(229,84,75,.12); color:#F5A9A3;}
  .vc-cite{display:inline-block; margin:0 2px; padding:1px 7px; border-radius:6px; font-size:12px;
    color:#0E1420; background:linear-gradient(180deg,#F0C260,#E8B23A); font-weight:600;}
  .vc-answer{font-size:15px; line-height:1.85; white-space:pre-wrap;}
  .vc-card{background:var(--panel2); border:1px solid var(--line); border-radius:12px;
    padding:10px 12px; margin:8px 0;}
  .vc-meta{color:var(--muted); font-size:12px;}
  .vc-group{font-size:12px; color:var(--muted); margin:12px 0 4px; letter-spacing:.4px;}
  .vc-card-h{font-size:18px; font-weight:600; margin:0 0 4px;}
  .vc-note{font-size:12px; color:var(--muted);}
  .vc-newchip{display:inline-block; margin:0 4px 4px 0; padding:2px 9px; border-radius:999px;
    font-size:12px; border:1px solid rgba(63,191,127,.55); background:rgba(63,191,127,.14); color:#9BE7BE;}
  .vc-timeline{display:flex; flex-direction:column; gap:6px;}
  .vc-row{display:grid; grid-template-columns:150px 1fr 78px; gap:10px; align-items:center;}
  .vc-node{font-size:12px; color:var(--text); overflow:hidden; text-overflow:ellipsis;}
  .vc-bar{height:10px; border-radius:6px; background:rgba(255,255,255,.06); overflow:hidden;}
  .vc-bar > i{display:block; height:100%; border-radius:6px;
    background:linear-gradient(90deg,var(--blue),var(--blue2));}
  .vc-bar.warn > i{background:linear-gradient(90deg,#E8B23A,#F3D68B);}
  .vc-bar.bad > i{background:linear-gradient(90deg,#E5544B,#F58A84);}
  .vc-ms{font-size:11px; color:var(--muted); text-align:right;}
  .vc-kv{display:flex; gap:18px; flex-wrap:wrap; font-size:12px; color:var(--muted);}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def chip(text: str, kind: str = "") -> str:
    return "<span class='vc-chip %s'>%s</span>" % (kind, html.escape(str(text)))


def render_answer(answer: str) -> str:
    """把 [Pxx] 渲染成金色角标（先转义再替换，避免注入）。"""
    safe = html.escape(answer or "")
    return re.sub(r"\[P(\d+)\]", r"<span class='vc-cite'>P\1</span>", safe)


def cite_chips(cites: list) -> str:
    """["P12","P30"] -> 金色角标串。"""
    out = []
    for c in cites or []:
        s = str(c).strip()
        m = re.fullmatch(r"[Pp](\d+)", s)
        if m:
            out.append("<span class='vc-cite'>P%s</span>" % m.group(1))
    return " ".join(out)


def render_trace(trace: list) -> None:
    if not trace:
        return
    mx = max([float(t.get("latency_ms", 0) or 0) for t in trace] + [1.0])
    rows = []
    for t in trace:
        ok = bool(t.get("ok", True))
        deg = (t.get("degraded") or "").strip()
        err = (t.get("error_code") or "").strip()
        cls = "bad" if (not ok or err) else ("warn" if deg else "")
        width = max(2.0, 100.0 * float(t.get("latency_ms", 0) or 0) / mx)
        node = html.escape(str(t.get("node", "")))
        tip = html.escape("%s%s" % (err or "", (" · " + deg) if deg else ""))
        rows.append(
            "<div class='vc-row'><div class='vc-node'>%s</div>"
            "<div class='vc-bar %s' title='%s'><i style='width:%.1f%%'></i></div>"
            "<div class='vc-ms'>%.0f ms</div></div>" % (node, cls, tip, width, float(t.get("latency_ms", 0) or 0))
        )
    st.markdown("<div class='vc-timeline'>%s</div>" % "".join(rows), unsafe_allow_html=True)


def render_evidence(result: dict) -> None:
    citations = result.get("citations") or []
    ctx = result.get("context") or []
    if citations:
        st.markdown("**引用来源（%d）**" % len(citations), unsafe_allow_html=True)
        for c in citations:
            st.markdown(
                "<div class='vc-card'><div><b>P%s</b> <span class='vc-meta'>· %s</span></div>"
                "<div class='vc-meta'>%s</div></div>"
                % (c.get("page"), html.escape(c.get("section_path", "") or "—"),
                   html.escape(clean_display(c.get("snippet", ""))[:220])),
                unsafe_allow_html=True,
            )
    if ctx:
        with st.expander("召回片段（%d）· 为什么是这些" % len(ctx)):
            for c in ctx:
                routes = "→".join(c.get("routes") or [c.get("source_route", "")])
                ranks = c.get("rank_by_route") or {}
                st.markdown(
                    "<div class='vc-card'><div><b>P%s</b> %s</div>"
                    "<div class='vc-meta'>score=%.4f | routes=%s | rank=%s | rerank=%s | table=%s</div>"
                    "<div class='vc-meta'>%s</div></div>"
                    % (c.get("page"), html.escape(c.get("section_path", "") or ""),
                       float(c.get("score", 0) or 0), html.escape(routes),
                       html.escape(json.dumps(ranks, ensure_ascii=False)),
                       c.get("rerank_score", "—"), "✔" if c.get("table_flag") else "—",
                       html.escape(clean_display(c.get("text", ""))[:300])),
                    unsafe_allow_html=True,
                )


# ---------------- 会话状态 ----------------
# 星图视图字段：服务端按轮次下发，前端按**键**合并（不是整体覆盖）。
# 连边轮只下发 clickable / session_edges，不下发 tiers；整体覆盖会让建边成功后
# 所有已点亮节点瞬间失档（档位变空 → 全掉回默认），看起来像系统故障。
SM_KEYS = ("unlocked_nodes", "clickable", "tiers", "session_edges")

if "thread_id" not in st.session_state:
    # 只生成一次：thread_id 变了就等于换了个会话，已点亮节点全丢
    st.session_state.thread_id = "ui_" + uuid.uuid4().hex[:8]
if "history" not in st.session_state:
    st.session_state.history = []
if "pending" not in st.session_state:
    st.session_state.pending = ""
if "star" not in st.session_state:
    st.session_state.star = {}
if "sm" not in st.session_state:
    # 首屏还没跑过任何 ask，ui_payload 是空的；此时若按"clickable 为空"渲染，
    # 全部节点都会是禁用态 —— 用户面对一张全灰的图，不知道能干什么。
    # starmap_state 是只读快照（不跑图、不检索、不生成），正是为首屏而存在。
    try:
        snap = starmap_state(st.session_state.thread_id)
        st.session_state.sm = {k: snap[k] for k in SM_KEYS if k in snap}
    except Exception:
        st.session_state.sm = {}
if "confirm_reset_all" not in st.session_state:
    st.session_state.confirm_reset_all = False


def star_result() -> dict:
    return st.session_state.star or {}


def sm_view() -> dict:
    """跨轮次合并后的星图视图（unlocked / clickable / tiers / session_edges）。"""
    sm = dict(st.session_state.get("sm") or {})
    payload = star_result().get("ui_payload") or {}
    for k in SM_KEYS:
        if k in payload:
            sm[k] = payload[k]
    # 顶层兜底：自然语言轮可能只在 state 上带 unlocked_nodes
    if "unlocked_nodes" not in sm:
        sm["unlocked_nodes"] = list(star_result().get("unlocked_nodes") or [])
    st.session_state.sm = sm
    return sm


def _scope_filters() -> dict:
    """星图交互都要带作用域，否则后端会发澄清卡片反问"你想看哪家公司"。"""
    f: dict = {}
    if st.session_state.get("sel_company"):
        f["company"] = st.session_state.sel_company
    if st.session_state.get("sel_period"):
        f["report_period"] = st.session_state.sel_period
    return f


def click_node(node_id: str, via: str = "") -> None:
    """点亮一个星图节点：只发 UI 事件，业务逻辑全在 ask() 里。

    via=text_link 用于系统给出的出口（推荐 / 下一跳 / 备选）：那是系统自己算出来的
    路径，再对它做邻接校验等于把用户刚看到的东西又锁上。
    """
    f: dict = {"node_id": node_id}
    if via:
        f["via"] = via
    f.update(_scope_filters())
    with st.spinner("点亮 → 召回 → 融合 → 生成 → 校验…"):
        st.session_state.star = ask(ui_action=UI_ACTION_STAR, ui_filters=f,
                                    thread_id=st.session_state.thread_id)
    # 本轮的星图按钮在 click 判定**之前**就渲染完了（用的是旧状态），不重跑的话
    # "已点亮"标记要等下一次交互才刷新，用户看起来就是"点了没反应"。
    st.rerun()


def link_action(node_id: str, target: str = "", confirmed: bool = False,
                token: str = "") -> None:
    """连边三段：target 留空 = 要候选；给了 target 未确认 = 要确认卡片；确认后才真建边。

    必须显式确认：连边会改变整张图的拓扑（进而改变别人的可点亮集合），
    一次误点就该默默改图的话，用户根本无从发现、也无从撤销。
    """
    f: dict = {"node_id": node_id}
    if target:
        f["link_target"] = target
    if confirmed:
        f["link_confirmed"] = True
    if token:
        f["candidates_token"] = token
    f.update(_scope_filters())
    with st.spinner("建立关联…"):
        st.session_state.star = ask(ui_action=UI_ACTION_LINK, ui_filters=f,
                                    thread_id=st.session_state.thread_id)
    st.rerun()


def reset_starmap(all_scope: bool = False) -> None:
    with st.spinner("重置星图…"):
        st.session_state.star = ask(
            ui_action=UI_ACTION_RESET_ALL if all_scope else UI_ACTION_RESET,
            thread_id=st.session_state.thread_id)
    # 快照必须一起清：否则重置后本轮 ui_payload 还没回来时，
    # 前端会拿上一轮的档位渲染出"进度已清零但节点还亮着"的矛盾画面。
    st.session_state.sm = {}
    st.session_state.confirm_reset_all = False
    st.rerun()


# 已点亮节点的档位符号。亮度由服务端按 bright/mid/dim 下发，前端**不自己算**——
# 两边各算一遍必然漂移，用户会看到"刚点亮的节点却不亮"。
TIER_MARK = {"bright": "●", "mid": "◐", "dim": "○"}


def node_button(node_id: str, label: str, view: dict, current: str,
                prefix_key: str = "star") -> None:
    """单个星图节点。状态用按钮文案 + type 表达（Streamlit 无法给单个 button 加 class）。"""
    unlocked = set(view.get("unlocked_nodes") or [])
    clickable = set(view.get("clickable") or [])
    rec = set(view.get("rec") or [])
    tiers = view.get("tiers") or {}

    if node_id == current:
        mark = "◉"
    elif node_id in rec:
        mark = "⭐"
    elif node_id in unlocked:
        mark = TIER_MARK.get(str(tiers.get(node_id) or "mid"), "◐")
    elif node_id in clickable:
        mark = "＋"
    else:
        mark = "🔒"

    # 不在前沿上的节点直接禁用：让它可点、点了才说"还没解锁"，
    # 用户会以为是自己点错了，而不是机制在起作用。
    locked = node_id not in unlocked and node_id not in clickable
    if st.button("%s %s" % (mark, label), key="%s_%s" % (prefix_key, node_id),
                 disabled=locked,
                 help="还没解锁，先点亮与它相邻的指标" if locked else None,
                 type="primary" if node_id in unlocked else "secondary",
                 use_container_width=True):
        click_node(node_id)


def render_starmap() -> None:
    s = star_result()
    session_nodes = s.get("session_nodes") or {}
    payload = s.get("ui_payload") or {}
    current = str(payload.get("node_id") or "")

    view = sm_view()
    view["rec"] = [str(x) for x in (payload.get("unlock_next") or [])]

    # 必须是**单级**三列：Streamlit 禁止列嵌套超过一层（在列里再开 st.columns 会直接抛
    # StreamlitAPIException，且只在运行时才暴露）。
    head = st.columns([3, 1, 1])
    with head[0]:
        st.markdown("### 🧭 知识星图")
    with head[1]:
        st.button("重置进度", key="btn_reset", use_container_width=True,
                  on_click=reset_starmap, kwargs={"all_scope": False},
                  help="只清点亮进度，你创建的新节点与连线都保留")
    with head[2]:
        # 清除全部会删掉用户自己造的节点和连线、且不可撤销 → 必须二次确认
        if st.session_state.get("confirm_reset_all"):
            st.button("确认清除？", key="btn_reset_all_go", use_container_width=True,
                      on_click=reset_starmap, kwargs={"all_scope": True})
        elif st.button("清除全部", key="btn_reset_all", use_container_width=True,
                       help="连新节点与连线一起清除，不可撤销"):
            st.session_state.confirm_reset_all = True
            st.rerun()

    st.caption("＋ 可点亮 ｜ 🔒 未解锁 ｜ ⭐ 推荐 ｜ ●◐○ 已点亮（按亮度） ｜ ◉ 当前")

    for name, _tone, ids in STAR_GROUPS:
        st.markdown("<div class='vc-group'>%s</div>" % html.escape(name), unsafe_allow_html=True)
        cols = st.columns(3)
        for i, nid in enumerate(ids):
            with cols[i % 3]:
                node_button(nid, label_for_node(nid, session_nodes), view, current)

    # 会话新节点：模型提议且通过可召回性验证的，单独成组，避免和字典节点混淆
    if session_nodes:
        st.markdown("<div class='vc-group'>本会话新发现（已通过可召回性验证）</div>",
                    unsafe_allow_html=True)
        cols = st.columns(3)
        for i, nid in enumerate(sorted(session_nodes.keys())):
            with cols[i % 3]:
                node_button(nid, label_for_node(nid, session_nodes), view, current,
                            prefix_key="sess")

    # 会话边：用户自己连的关联，必须看得见 —— 否则点了"已连接"却无从确认，
    # 下次看到拓扑变了也不知道是自己造成的。
    edges = [e for e in (view.get("session_edges") or []) if isinstance(e, dict)]
    if edges:
        st.markdown("<div class='vc-group'>你创建的关联（%d/%d）</div>"
                    % (len(edges), CONFIG.max_session_edges), unsafe_allow_html=True)
        st.markdown(" ".join(
            chip("%s ↔ %s%s" % (label_for_node(str(e.get("from") or ""), session_nodes),
                                label_for_node(str(e.get("to") or ""), session_nodes),
                                " · 系统" if str(e.get("origin") or "") == "llm" else ""),
                 "green")
            for e in edges), unsafe_allow_html=True)


LINK_TYPES = ("link_candidates", "link_confirm_card", "link_done",
             "link_exists", "link_open", "link_failed")


def _render_link_zone(payload: dict, session_nodes: dict) -> None:
    """连边交互区：服务端回什么卡片就渲染什么，前端**不做任何判定**。

    候选降权、可用性、上限、token 校验全在 link.py。前端若自己也判一遍，
    就会出现"按钮能点、服务端却拒绝"的错位 —— 那比禁用按钮更难解释。
    """
    src = str(payload.get("node_id") or "")
    # 没选节点、或当前是被锁定的卡片（它没点亮，不能当连线起点）
    if not src or payload.get("locked") or payload.get("reset"):
        return

    t = str(payload.get("type") or "")
    used = len([e for e in (sm_view().get("session_edges") or []) if isinstance(e, dict)])
    with st.expander("🔗 与「%s」建立关联（%d/%d）"
                     % (label_for_node(src, session_nodes), used, CONFIG.max_session_edges),
                     expanded=t in LINK_TYPES):
        if st.button("查看可关联的指标", key="link_ask", use_container_width=True):
            link_action(src)
            return

        if t == "link_candidates":
            opts = [str(x) for x in (payload.get("options") or []) if x]
            if payload.get("stale"):
                st.caption("星图已经变了，请重新选择。")
            if not opts:
                st.caption("暂时找不到适合与它关联的指标。")
            for nid in opts:
                if st.button("↔ " + label_for_node(nid, session_nodes),
                             key="linkopt_" + nid, use_container_width=True):
                    link_action(src, target=nid)

        elif t == "link_confirm_card":
            tgt = str(payload.get("link_target") or "")
            st.markdown(
                "连接 **%s** ↔ **%s**？"
                % (html.escape(label_for_node(src, session_nodes)),
                   html.escape(str(payload.get("link_label") or
                                   label_for_node(tgt, session_nodes)))))
            c1, c2 = st.columns(2)
            with c1:
                if st.button("确认连接", key="link_yes", use_container_width=True):
                    link_action(src, target=tgt, confirmed=True,
                                token=str(payload.get("candidates_token") or ""))
            with c2:
                if st.button("换个指标", key="link_no", use_container_width=True):
                    link_action(src)

        elif t == "link_open":
            # 目标已点亮：直接打开它比建一条无意义的边更贴合意图
            st.caption(str(star_result().get("final_answer") or ""))
            if st.button("打开 " + label_for_node(str(payload.get("node_id") or ""),
                                                  session_nodes),
                         key="link_open_go", use_container_width=True):
                click_node(str(payload.get("node_id") or ""), via=VIA_TEXT_LINK)

        elif t in LINK_TYPES:
            st.caption(str(star_result().get("final_answer") or ""))


def render_star_card() -> None:
    s = star_result()
    payload = s.get("ui_payload") or {}
    session_nodes = s.get("session_nodes") or {}
    if not payload:
        st.info("点击左侧星图节点开始探索，或在下方输入框直接提问。")
        return

    scope = payload.get("scope") or {}
    scope_txt = " · ".join(
        x for x in [scope.get("company"),
                    period_cn(scope.get("report_period")) if scope.get("report_period") else ""] if x)
    st.markdown("### 📄 节点卡片")
    st.markdown(
        "<div class='vc-wrap'><div class='vc-card-h'>%s</div>"
        "<div class='vc-note'>%s</div>"
        "<div class='vc-answer' style='margin-top:8px'>%s</div></div>"
        % (html.escape(str(payload.get("label") or payload.get("node_id") or "")),
           html.escape(scope_txt or "未锁定公司/期间"),
           render_answer(str(payload.get("explanation") or ""))),
        unsafe_allow_html=True,
    )

    by_period = payload.get("citations_by_period") or {}
    if by_period:
        # 对比模式：按期间分组给出页码，避免两期同页时指错期
        rows = []
        for p, pages in by_period.items():
            rows.append("<div class='vc-meta'>%s：%s</div>"
                        % (html.escape(period_cn(p)), cite_chips(pages)))
        st.markdown("<div class='vc-card'>%s</div>" % "".join(rows), unsafe_allow_html=True)

    if payload.get("refused"):
        st.markdown(chip("该节点在本期报告中未找到可溯源数据", "gold"), unsafe_allow_html=True)

    # ---- 锁定引导：不能只说"没解锁"，必须给出口 ----
    # 只给一句"还没解锁"等于把用户丢在原地：他不知道下一步该点哪个。
    if payload.get("locked"):
        hop = str(payload.get("next_hop") or "")
        alts = [str(x) for x in (payload.get("alternatives") or []) if x]
        outs = ([hop] if hop else []) + [a for a in alts if a != hop]
        if outs:
            st.markdown("**可以先看**")
            cols = st.columns(len(outs))
            for i, nid in enumerate(outs):
                with cols[i]:
                    if st.button("▸ " + label_for_node(nid, session_nodes),
                                 key="hop_" + nid, use_container_width=True):
                        click_node(nid, via=VIA_TEXT_LINK)
        else:
            st.caption("继续点亮相邻指标，就会解锁更多。")

    recs = [str(x) for x in (payload.get("unlock_next") or []) if x]
    if recs:
        st.markdown("**推荐点亮**")
        cols = st.columns(len(recs))
        for i, nid in enumerate(recs):
            with cols[i]:
                if st.button("⭐ " + label_for_node(nid, session_nodes), key="rec_" + nid,
                             use_container_width=True):
                    click_node(nid, via=VIA_TEXT_LINK)

    _render_link_zone(payload, session_nodes)

    news = payload.get("new_nodes") or []
    if news:
        st.markdown("**本轮新发现**")
        st.markdown("".join("<span class='vc-newchip'>新 · %s</span>"
                            % html.escape(str(n.get("label") or n.get("id") or ""))
                            for n in news), unsafe_allow_html=True)
        st.caption("模型提议的科目，已通过可召回性验证才进星图（只在本会话有效）。")

    degraded = sorted(set(s.get("degraded") or []))
    if degraded:
        st.markdown(chip("降级：" + "、".join(degraded), "red"), unsafe_allow_html=True)

    # 不能包在 st.expander 里：render_evidence 内部自带 expander，
    # Streamlit 禁止 expander 嵌套（运行时才报错，静态检查看不出来）。
    st.markdown("**该节点的检索与证据**")
    st.markdown("<div class='vc-kv'><span>检索词：%s</span><span>已点亮：%d</span></div>"
                % (html.escape(str(s.get("query_rewritten") or "")),
                   len(s.get("unlocked_nodes") or [])), unsafe_allow_html=True)
    render_evidence(s)
    render_trace(s.get("trace") or [])


# ---------------- 侧栏 ----------------
with st.sidebar:
    st.markdown("### 控制台")
    docs = all_docs()
    if not docs:
        st.warning("尚未入库，请先执行：python3 scripts/ingest.py")
    for d in docs:
        st.markdown(
            "**%s**（%s）\n\n期间 %s ｜ %s chunks ｜ `%s`"
            % (d.get("company", "?"), d.get("stock_code", "-"), d.get("report_period", "-"),
               d.get("chunk_count", 0), d.get("doc_version", "-")),
        )
    ver = verify_index()
    st.markdown(chip("落盘一致" if ver.get("ok") else "落盘不一致",
                     "green" if ver.get("ok") else "red"), unsafe_allow_html=True)
    st.caption("docs=%s chunks=%s provider=%s dim=%s"
               % (ver.get("docs"), ver.get("chunks"), ver.get("provider"), ver.get("dim")))

    st.divider()
    # 作用域选择：星图点击不带 company 时后端会发澄清卡片，这里让用户先锁定，
    # 避免每次点节点都被反问一句"你想看哪家公司"
    st.markdown("**星图作用域**")
    companies = list(dict.fromkeys(
        [str(d.get("company") or d.get("short_name") or "") for d in docs]))
    companies = [c for c in companies if c]
    periods = sorted(set(str(d.get("report_period") or "") for d in docs if d.get("report_period")))
    if companies:
        st.selectbox("公司", companies, key="sel_company")
    else:
        st.session_state.sel_company = ""
    if periods:
        st.selectbox("报告期", periods, key="sel_period",
                     format_func=lambda p: "%s（%s）" % (period_cn(p), p))
    else:
        st.session_state.sel_period = ""

    st.divider()
    st.markdown("**运行时参数**")
    CONFIG.top_k_final = st.slider("最终上下文 Top-K", 3, 12, int(CONFIG.top_k_final))
    CONFIG.min_top1_relevance = st.slider("相关性闸门阈值", 0.10, 0.60, float(CONFIG.min_top1_relevance), 0.02)
    CONFIG.rerank_provider = st.radio("重排模型", ["heuristic", "bge", "none"],
                                      index=["heuristic", "bge", "none"].index(CONFIG.rerank_provider)
                                      if CONFIG.rerank_provider in ("heuristic", "bge", "none") else 0,
                                      help="bge = BAAI/bge-reranker-base（需 torch，首次加载较慢）")
    st.caption("embedding=%s ｜ llm=%s ｜ store=%s"
               % (CONFIG.embedding_provider, CONFIG.llm_provider, CONFIG.vector_store_provider))

    st.divider()
    st.markdown("**示例问题**")
    for s in SUGGESTIONS:
        if st.button(s, key="sug_" + s, use_container_width=True):
            st.session_state.pending = s
    st.divider()
    st.caption("所有回答基于公开财报文本自动提取，不构成投资建议。")

# ---------------- 顶栏 ----------------
meta = known_meta()
st.markdown(
    "<div class='vc-wrap'><div class='vc-top'>"
    "<div><p class='vc-title'>🧭 value_compass</p>"
    "<div class='vc-sub'>%s ｜ %s ｜ 全链路可溯源财报问答</div></div>"
    "%s%s%s%s</div></div>"
    % (html.escape(meta.get("company", "知识库") or "知识库"),
       html.escape(meta.get("report_period", "") or ""),
       chip("embedding: %s" % CONFIG.embedding_provider, "gold"),
       chip("llm: %s" % CONFIG.llm_provider),
       chip("rerank: %s" % CONFIG.rerank_provider),
       chip("chunks: %s" % (ver.get("chunks") or 0), "green")),
    unsafe_allow_html=True,
)

# ---------------- 星图 + 卡片 ----------------
left, right = st.columns([1, 1.15])
with left:
    render_starmap()
with right:
    render_star_card()

st.divider()


def render_turn(q: str, result: dict) -> None:
    with st.chat_message("assistant"):
        st.markdown("<div class='vc-answer'>%s</div>" % render_answer(result.get("final_answer", "")),
                    unsafe_allow_html=True)
        intent = result.get("intent", "")
        stats = result.get("fusion_stats") or {}
        st.markdown(
            "<div class='vc-kv'><span>意图：%s（%s）</span><span>融合：%s 片段</span>"
            "<span>top1 相关性：%s</span><span>命中路由：%s</span><span>retry：%s</span></div>"
            % (html.escape(str(intent)), result.get("intent_confidence", ""),
               stats.get("count", 0), stats.get("top1_relevance", "—"),
               html.escape(json.dumps(stats.get("route_hits", {}), ensure_ascii=False)),
               result.get("retry_count", 0)),
            unsafe_allow_html=True,
        )
        degraded = sorted(set(result.get("degraded") or []))
        if degraded:
            st.markdown(chip("降级：" + "、".join(degraded), "red"), unsafe_allow_html=True)
        if result.get("fallback_reason"):
            st.markdown(chip("兜底：" + str(result["fallback_reason"]), "gold"), unsafe_allow_html=True)

        col1, col2 = st.columns([1.1, 1])
        with col1:
            render_evidence(result)
        with col2:
            st.markdown("**执行 trace（节点耗时）**")
            render_trace(result.get("trace") or [])
            with st.expander("原始状态 JSON"):
                st.json({
                    "trace_id": result.get("trace_id"),
                    "intent": intent,
                    "intent_confidence": result.get("intent_confidence"),
                    "intent_scores": result.get("intent_scores"),
                    "fusion_stats": stats,
                    "gate_pass": result.get("gate_pass"),
                    "retry_count": result.get("retry_count"),
                    "errors": result.get("errors", []),
                    "degraded": degraded,
                })


for turn in st.session_state.history:
    with st.chat_message("user"):
        st.write(turn["q"])
    render_turn(turn["q"], turn["r"])

prompt = st.chat_input("问点什么，例如：公司本期营业收入是多少？") or st.session_state.pending
if prompt:
    st.session_state.pending = ""
    with st.chat_message("user"):
        st.write(prompt)
    with st.spinner("检索 → 融合 → 重排 → 生成 → 校验…"):
        flat: list = []
        for t in st.session_state.history:
            flat.append({"role": "user", "content": t["q"]})
            flat.append({"role": "assistant", "content": t["r"].get("final_answer", "")})
        result = ask(prompt, history=flat, thread_id=st.session_state.thread_id)
    render_turn(prompt, result)
    st.session_state.history.append({"q": prompt, "r": result})
