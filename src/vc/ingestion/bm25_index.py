"""BM25 倒排索引：构建 / 落盘 / 加载 / 检索。

与向量库共用同一份 chunk 快照（manifest 中的 doc_version 保证一致），
避免两路召回内容错位导致引用页码对不上。
"""
from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import CONFIG
from ..text_utils import strip_page_prefix, tokenize

_BM25_CLS = None
try:  # 优先用 rank_bm25；不可用时回落到内置实现
    from rank_bm25 import BM25Okapi as _BM25_CLS  # type: ignore
except Exception:  # pragma: no cover
    _BM25_CLS = None


class _SimpleBM25:
    """rank_bm25 缺失时的极简 BM25 兜底（k1=1.5, b=0.75）。"""

    def __init__(self, corpus: List[List[str]]):
        self.docs = corpus
        self.k1, self.b = 1.5, 0.75
        self.doc_len = [len(d) for d in corpus]
        self.avgdl = (sum(self.doc_len) / len(corpus)) if corpus else 0.0
        df: Dict[str, int] = {}
        for d in corpus:
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        self.df = df
        self.N = len(corpus)

    def _idf(self, t: str) -> float:
        n = self.df.get(t, 0)
        return max(0.0, (self.N - n + 0.5) / (n + 0.5) + 1.0).__float__() if n else 0.0

    def get_scores(self, query: List[str]) -> List[float]:
        import math

        out = []
        for i, d in enumerate(self.docs):
            tf: Dict[str, int] = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            dl = self.doc_len[i] or 1
            for t in set(query):
                if t not in tf:
                    continue
                idf = math.log(1 + (self.N - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))
                denom = tf[t] + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                score += idf * tf[t] * (self.k1 + 1) / denom
            out.append(score)
        return out


class BM25Index:
    def __init__(self, chunks: Optional[List[Dict[str, Any]]] = None):
        self.chunks: List[Dict[str, Any]] = list(chunks or [])
        self._model = None
        self.df: Dict[str, int] = {}
        self.n_docs = 0
        # 同一 query 的 BM25 分数缓存：bm25_recall 与 metadata_recall 是并行 Send 出去的
        # 两路，却对同一个 query 各算一遍全量打分（56755 chunk，实测 1.0–1.6s/次）。
        # 两路抢 GIL 串行累加后超过 timeout_bm25，双双被判超时 -> 召回塌成一路 -> 转兜底。
        # 缓存让第二路直接复用，省掉一半耗时。
        self._score_cache: Dict[Tuple[str, ...], List[float]] = {}
        if self.chunks:
            self.build(self.chunks)

    def _scores(self, q: List[str]) -> List[float]:
        key = tuple(q)
        hit = self._score_cache.get(key)
        if hit is None:
            hit = self._model.get_scores(q)
            if len(self._score_cache) >= 8:      # 单条 ≈1.8MB（56755 float），限 8 条约 14MB
                self._score_cache.clear()
            self._score_cache[key] = hit
        return hit

    def build(self, chunks: List[Dict[str, Any]]) -> None:
        from collections import Counter

        self.chunks = list(chunks)
        corpus = [tokenize(c.get("text", "")) for c in self.chunks]
        self.df: Dict[str, int] = Counter()
        for toks in corpus:
            self.df.update(set(toks))
        self.n_docs = len(corpus)
        if not corpus:
            self._model = None
            return
        self._model = (_BM25_CLS(corpus) if _BM25_CLS else _SimpleBM25(corpus))

    def idf(self, term: str) -> float:
        import math

        n = float(self.n_docs or 0)
        if not n:
            return 0.0
        df = self.df.get(term, 0)
        if not df:
            return math.log(1.0 + n)  # 语料中完全没出现 -> 最大权重
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def idf_coverage(self, query_terms, text: str) -> float:
        """IDF 加权相关性：命中罕见词才算真相关。

        三个关键设计（都是踩坑后改的）：
        1. **IDF 加权**：普通字 bigram 覆盖率在 800 字长块上基线就有 0.15~0.25，
           会把"量子计算对本财报的影响"误判为相关；加权后命中"的/影响"这类常见词贡献极低。
        2. **只统计语料中真实存在的词（df>0）**：问句里的"入是/是多/多少"这类过渡 bigram
           在语料里 df=0，会被算成最大 IDF 且永远命中不了，把相关性压到 0.1 以下。
        3. **若一个词都不在语料中 → 0.0**：例如"量子/计算"这份财报里根本没有，
           说明问题超出知识库范围，直接判不相关。
        """
        if not self.n_docs or not query_terms:
            return 0.0

        terms = [t for t in query_terms if self.df.get(t, 0) > 0]
        if not terms:
            return 0.0  # 问题里的词在知识库中一个都不存在 -> 超出范围

        doc = set(tokenize(strip_page_prefix(text)))
        if not doc:
            return 0.0
        num = sum(self.idf(t) for t in terms if t in doc)
        den = sum(self.idf(t) for t in terms) or 1.0
        return num / den

    def search(self, query: str, top_k: int = 20) -> List[Tuple[int, float]]:
        if not self._model or not self.chunks:
            return []
        q = tokenize(query)
        if not q:
            return []
        scores = self._scores(q)
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_k]
        mx = max(scores) or 1.0
        return [(i, float(scores[i]) / float(mx)) for i in ranked if scores[i] > 0]

    def search_where(self, query: str, matches, top_k: int = 20) -> List[Tuple[int, float]]:
        """**过滤优先**的检索：先按元数据圈定范围，再在范围内按 BM25 排序。

        `search()` 之后再做后置过滤在多文档语料下会失效：公司名/证券简称常出现在页眉
        而被 `clean_normalize` 当噪声清掉，于是「宁德时代净利润」的 BM25 top-N 里
        可能一条宁德时代的 chunk 都没有，过滤后直接空——范围这一路等于白跑。
        """
        if not self._model or not self.chunks:
            return []
        q = tokenize(query)
        if not q:
            return []
        scores = self._scores(q)
        ranked = sorted(
            (i for i in range(len(scores)) if scores[i] > 0 and matches(self.chunks[i])),
            key=lambda i: -scores[i],
        )[:top_k]
        if not ranked:
            return []
        mx = max(scores) or 1.0
        return [(i, float(scores[i]) / float(mx)) for i in ranked]

    def save(self, path: str = None) -> None:
        p = Path(path or CONFIG.bm25_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"chunks": self.chunks}, f)
        tmp.replace(p)

    @classmethod
    def load(cls, path: str = None) -> "BM25Index":
        p = Path(path or CONFIG.bm25_path)
        if not p.exists():
            return cls([])
        with open(p, "rb") as f:
            data = pickle.load(f)
        idx = cls([])
        idx.build(data.get("chunks", []))
        return idx

    def __len__(self) -> int:
        return len(self.chunks)


_CACHE: Dict[str, BM25Index] = {}


def get_bm25(refresh: bool = False) -> BM25Index:
    if refresh or "idx" not in _CACHE:
        _CACHE["idx"] = BM25Index.load()
    return _CACHE["idx"]
