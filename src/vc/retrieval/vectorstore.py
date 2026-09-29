"""向量库抽象：ChromaStore（默认） + PickleStore（降级）。

降级触发：chromadb 不可用 / Python 版本不兼容 / 索引目录损坏。
两者接口一致，上层（召回节点）无感，通过 VECTOR_STORE_PROVIDER 切换。
"""
from __future__ import annotations

import math
import pickle
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import CONFIG
from ..state import Chunk

_META_KEYS = [
    "chunk_id", "doc_id", "doc_version", "company", "short_name", "stock_code",
    "report_period", "report_type", "industry", "page", "section_path",
    "table_flag", "content_hash", "source_path", "source_url",
]


def _collection_suffix() -> str:
    """集合名带 provider+dim 后缀。

    Chroma 持久化集合的向量维度是**创建时固定**的：hashing(384) 换成 bge(512) 后
    往同一个集合 upsert 会直接失败。用后缀隔离，天然实现"换模型即换空间"，
    旧数据保留（可回滚），plan_diff 也会因 dim 变化判定为 full 全量重建。
    """
    try:
        from ..providers import get_embedding_provider

        emb = get_embedding_provider()
        return "__%s_%d" % (emb.name, int(getattr(emb, "dim", 0) or 0))
    except Exception:
        return ""


def _safe_meta(c: Dict[str, Any]) -> Dict[str, Any]:
    """Chroma metadata 只接受 str/int/float/bool，且不接受 None。"""
    out: Dict[str, Any] = {}
    for k in _META_KEYS:
        v = c.get(k)
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = bool(v)
        elif isinstance(v, (int, float)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


class VectorStore:
    name = "base"

    def upsert(self, chunks: List[Dict[str, Any]], vectors: List[List[float]]) -> int:
        raise NotImplementedError

    def query(self, vector: List[float], top_k: int = 20, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def delete_by_doc(self, doc_id: str) -> int:
        raise NotImplementedError

    def delete_where(self, where: Dict[str, Any]) -> int:
        """按条件删除（用于 page 级增量更新）；不支持时抛异常由调用方降级。"""
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError


class ChromaStore(VectorStore):
    name = "chroma"

    def __init__(self, persist_dir: str = None, collection: str = "vc_chunks"):
        import chromadb
        from chromadb.config import Settings

        self._dir = str(persist_dir or CONFIG.chroma_dir)
        Path(self._dir).mkdir(parents=True, exist_ok=True)
        self._collection_name = "%s%s" % (collection, _collection_suffix())
        self._client = chromadb.PersistentClient(
            path=self._dir,
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name, metadata={"hnsw:space": "cosine"}
        )

    def upsert(self, chunks: List[Dict[str, Any]], vectors: List[List[float]]) -> int:
        if not chunks:
            return 0
        self._collection.upsert(
            ids=[c["chunk_id"] for c in chunks],
            embeddings=[list(map(float, v)) for v in vectors],
            documents=[c.get("text", "") for c in chunks],
            metadatas=[_safe_meta(c) for c in chunks],
        )
        return len(chunks)

    def query(self, vector: List[float], top_k: int = 20, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "query_embeddings": [list(map(float, vector))],
            "n_results": int(top_k),
        }
        if where:
            kwargs["where"] = where
        res = self._collection.query(**kwargs)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: List[Dict[str, Any]] = []
        for i, doc in enumerate(docs):
            meta = dict(metas[i] or {})
            item = dict(meta)
            item["text"] = doc or ""
            item["score"] = float(1.0 - float(dists[i])) if i < len(dists) else 0.0
            out.append(item)
        return out

    def delete_by_doc(self, doc_id: str) -> int:
        try:
            res = self._collection.get(where={"doc_id": doc_id})
            ids = res.get("ids") or []
            if ids:
                self._collection.delete(ids=ids)
            return len(ids)
        except Exception:
            return 0

    def delete_where(self, where: Dict[str, Any]) -> int:
        res = self._collection.get(where=where)
        ids = res.get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception:
            return 0


class PickleStore(VectorStore):
    """纯 Python 降级实现：内存 dict + 余弦相似度 + pickle 落盘。"""

    name = "pickle"

    def __init__(self, path: str = None):
        self.path = Path(path or (CONFIG.index_dir / ("vectors%s.pkl" % (_collection_suffix() or "_default"))))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, Any]] = {}
        if self.path.exists():
            try:
                with open(self.path, "rb") as f:
                    self._data = pickle.load(f)
            except Exception:
                self._data = {}

    def _flush(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(self._data, f)
        tmp.replace(self.path)

    def upsert(self, chunks: List[Dict[str, Any]], vectors: List[List[float]]) -> int:
        for c, v in zip(chunks, vectors):
            item = dict(c)
            item["_vec"] = list(map(float, v))
            self._data[c["chunk_id"]] = item
        self._flush()
        return len(chunks)

    def query(self, vector: List[float], top_k: int = 20, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        q = list(map(float, vector))
        qn = math.sqrt(sum(x * x for x in q)) or 1.0
        scored = []
        for item in self._data.values():
            if where and not all(str(item.get(k)) == str(v) for k, v in where.items()):
                continue
            v = item.get("_vec") or []
            if len(v) != len(q):
                continue
            dot = sum(a * b for a, b in zip(v, q))
            vn = math.sqrt(sum(x * x for x in v)) or 1.0
            scored.append((dot / (vn * qn), item))
        scored.sort(key=lambda x: -x[0])
        out = []
        for s, item in scored[:top_k]:
            d = {k: v for k, v in item.items() if k != "_vec"}
            d["score"] = float(s)
            out.append(d)
        return out

    def delete_by_doc(self, doc_id: str) -> int:
        keys = [k for k, v in self._data.items() if v.get("doc_id") == doc_id]
        for k in keys:
            self._data.pop(k, None)
        if keys:
            self._flush()
        return len(keys)

    def delete_where(self, where: Dict[str, Any]) -> int:
        def match(item: Dict[str, Any]) -> bool:
            for k, v in where.items():
                if isinstance(v, dict) and "$in" in v:
                    if item.get(k) not in v["$in"]:
                        return False
                elif item.get(k) != v:
                    return False
            return True

        keys = [k for k, v in self._data.items() if match(v)]
        for k in keys:
            self._data.pop(k, None)
        if keys:
            self._flush()
        return len(keys)

    def count(self) -> int:
        return len(self._data)


_CACHE: Dict[str, VectorStore] = {}


def get_vector_store(provider: str = None, allow_fallback: bool = True) -> VectorStore:
    key = provider or CONFIG.vector_store_provider
    if key in _CACHE:
        return _CACHE[key]
    store: Optional[VectorStore] = None
    try:
        if key == "pickle":
            store = PickleStore()
        else:
            store = ChromaStore()
    except Exception:
        if not allow_fallback:
            raise
        store = PickleStore()
    _CACHE[key] = store
    return store
