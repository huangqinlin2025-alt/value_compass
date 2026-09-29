"""create_commit：用户主动新建星图节点。

两种结果，取决于内容是否到位：
  - 有关键词且通过可召回性探针 → 就绪节点，可点亮；
  - 关键词为空 / 未通过探针 → **草稿节点**（占位，内容待补）。

为什么不再一律拒绝：用户会先占位置（比如只填公司名、其余后补），
这是真实用法，硬挡掉等于逼他先把内容想全才能建。但草稿节点**不可点亮**——
空节点没有检索锚点，点亮它只会拿 node_id 去召回不相干的片段，
那正是"造词幽灵节点"的老毛病。所以：想建就建，能不能亮另说。

已存在的草稿节点可以被再次提交**升级**：补上关键词并过探针即转成就绪节点。
"""
from __future__ import annotations

import re
from typing import Any, Dict

from ...decorators import safe_node
from ...retrieval.probe import probe_node
from ...ui import DRAFT_STATUS, node_is_draft, node_meta

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def _reject(reason: str, hint: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"type": "create_rejected", "reason": reason, "hint": hint}
    payload.update(extra)
    return {
        "final_answer": hint,
        "ui_payload": payload,
        "errors": [],
        "trace": [],
    }


def _ok(status: str, nid: str, label: str, kws, hit: float, text: str,
        session_nodes: Dict[str, Any]) -> Dict[str, Any]:
    payload = {"type": "node_created", "node_id": nid, "label": label, "keywords": kws,
               "probe_hit": hit, "content_status": status, "hint": text}
    return {
        "session_nodes": session_nodes,
        "answer": text,          # output_guard 读的是 answer，只写 final_answer 会被兜底覆盖
        "final_answer": text,
        "ui_payload": payload,
        "errors": [],
        "trace": [],
    }


@safe_node("create_commit", timeout=8.0,
           fallback_patch={"final_answer": "新建节点失败，请稍后再试。",
                           "ui_payload": {"type": "create_rejected", "reason": "internal"}})
def create_commit(state: Dict[str, Any]) -> Dict[str, Any]:
    f = state.get("ui_filters") or {}
    nid = str(f.get("node_id") or "").strip().lower()
    label = str(f.get("label") or "").strip()
    kws = [str(k).strip() for k in (f.get("keywords") or []) if str(k).strip()]

    if not nid or not _ID_RE.match(nid):
        return _reject("bad_id", "节点 id 需为小写英文/数字/下划线（如 inventory_turnover），请修改后重试。")
    if not label:
        return _reject("no_label", "请填写节点中文名。")

    session_nodes: Dict[str, Any] = dict(state.get("session_nodes") or {})
    prev = node_meta(nid, session_nodes)
    if prev and not node_is_draft(nid, session_nodes):
        # 字典节点或已就绪的会话节点：重复建没有意义；草稿节点则落到下面走"补充升级"
        return _reject("exists", "「%s」已在星图中，无需重复创建。" % label,
                       node_id=nid, label=label)

    # 探针只在有关键词时才有意义：没给关键词就是明确的"内容待补"，不必再查一次
    hit = 0.0
    verified = False
    if kws:
        try:
            ok, h, _cov = probe_node(kws[:3], filters=state.get("filters") or {})
            hit = float(h or 0.0)
            verified = bool(ok)
        except Exception:
            # 探针出错不判"召回不到"：降级成草稿，用户至少保住了这个位置
            hit, verified = 0.0, False
    draft = not verified

    meta = {"label": label[:20], "keywords": kws[:3], "intent": "METRIC",
            "probe_hit": hit, "origin": "user"}
    if draft:
        meta["content_status"] = DRAFT_STATUS
    session_nodes[nid] = meta

    if draft and not kws:
        text = ("已新建占位节点「%s」。它还没有内容，暂时点不亮——"
                "之后补上关键词（报告里会出现的表述）即可点亮。" % label)
    elif draft:
        text = ("已新建占位节点「%s」，但关键词命中率只有 %.2f，报告里没出现这些表述，"
                "所以它暂时点不亮。换成报告原文会出现的说法即可点亮。" % (label, hit))
    else:
        text = ("已新建节点「%s」（关键词命中率 %.2f）。它还没有点亮，点一下即可查看内容。"
                % (label, hit))
    return _ok(DRAFT_STATUS if draft else "ready", nid, label, kws[:3], hit, text,
               session_nodes)
