"""Rerank 适配器。

默认 HeuristicReranker（零依赖）：
score = 0.45*关键词覆盖 + 0.25*数值命中 + 0.15*表格加权 + 0.15*页码邻近
后续可替换为 CrossEncoder（bge-reranker），接口不变。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..config import CONFIG
from ..text_utils import coverage_score, extract_numbers, strip_page_prefix, token_set


class Reranker:
    name = "base"
    # 由工厂在"降级"时写入原因，供 rerank 节点标记 degraded
    fallback_reason = ""

    def rerank(self, query: str, chunks: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
        raise NotImplementedError


class HeuristicReranker(Reranker):
    name = "heuristic"

    def rerank(self, query: str, chunks: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
        if not chunks:
            return []
        q_terms = token_set(query)
        q_nums = set(extract_numbers(query))
        top_page = _dominant_page(chunks)
        out = []
        for c in chunks:
            text = strip_page_prefix(c.get("text", ""))
            s = 0.45 * coverage_score(q_terms, text)
            if q_nums:
                hit = q_nums & set(extract_numbers(text))
                s += 0.25 * (len(hit) / float(len(q_nums)))
            elif re_has_number(text):
                s += 0.05
            if c.get("table_flag"):
                s += 0.15
            if top_page is not None and c.get("page") is not None:
                s += 0.15 * (1.0 / (1.0 + abs(int(c["page"]) - int(top_page))))
            s += 0.30 * float(c.get("score") or 0.0) * 10  # 融合分（RRF 通常 <0.05）放大后参与
            cc = dict(c)
            cc["rerank_score"] = round(s, 4)
            out.append(cc)
        out.sort(key=lambda x: -x["rerank_score"])
        return out[:top_k]


def re_has_number(text: str) -> bool:
    import re

    return bool(re.search(r"\d", text or ""))


def _dominant_page(chunks: List[Dict[str, Any]]) -> Any:
    pages = [c.get("page") for c in chunks[:5] if c.get("page") is not None]
    if not pages:
        return None
    return max(set(pages), key=pages.count)


class NoOpReranker(Reranker):
    name = "none"

    def rerank(self, query: str, chunks: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
        return list(chunks[:top_k])


class CrossEncoderReranker(Reranker):
    """真实重排模型（BAAI/bge-reranker-base，CrossEncoder 逐对打分）。

    只对融合后的 top-N（默认 20）打分，避免把重排成本放大到全库；
    加载失败由工厂回落 HeuristicReranker，链路不中断。
    """

    name = "cross_encoder"

    def __init__(self, model_name: str = None, device: str = None, batch_size: int = 16):
        from sentence_transformers import CrossEncoder

        from .embedding import pick_device, resolve_model_path

        self.model_name = model_name or CONFIG.rerank_model
        self.batch_size = max(1, int(batch_size))
        path = resolve_model_path(self.model_name)
        self._model = CrossEncoder(path, max_length=512, device=pick_device(device))

    def rerank(self, query: str, chunks: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
        if not chunks:
            return []
        pairs = [(query or "", strip_page_prefix(c.get("text", ""))[:512]) for c in chunks]
        scores = self._model.predict(pairs, batch_size=self.batch_size, show_progress_bar=False)
        out: List[Dict[str, Any]] = []
        for c, s in zip(chunks, scores):
            cc = dict(c)
            cc["rerank_score"] = round(float(s), 4)
            cc["rerank_provider"] = "cross_encoder"
            out.append(cc)
        out.sort(key=lambda x: -x["rerank_score"])
        return out[:top_k]


_RERANKER_CACHE: Dict[str, Reranker] = {}


def get_reranker() -> Reranker:
    key = (CONFIG.rerank_provider or "heuristic").strip().lower()
    if key in _RERANKER_CACHE:
        return _RERANKER_CACHE[key]

    reranker: Reranker
    if key in ("none", "noop"):
        reranker = NoOpReranker()
    elif key in ("bge", "cross_encoder", "crossencoder", "reranker", "bge_reranker"):
        try:
            reranker = CrossEncoderReranker()
        except Exception as exc:  # 模型缺失 / torch 不可用 -> 零依赖启发式兜底
            reranker = HeuristicReranker()
            reranker.fallback_reason = "cross_encoder 不可用(%s)" % str(exc)[:120]
    else:
        reranker = HeuristicReranker()
    _RERANKER_CACHE[key] = reranker
    return reranker
