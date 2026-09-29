"""Embedding 适配器。

- HashingEmbedding（默认）：零依赖、确定性、跨进程稳定（用 md5 而非内置 hash）。
  语义表达弱于 BGE，但保证"零密钥零外网"也能端到端跑通全链路。
- BGEEmbedding：需 sentence-transformers + 权重，失败自动回落 Hashing。
- OpenAICompatEmbedding：任何 OpenAI 兼容 /embeddings 服务。

切换提供者后索引维度可能变化 → manifest 检测到 dim/provider 变化会强制全量重建。
"""
from __future__ import annotations

import math
import os
from collections import Counter
from pathlib import Path
from typing import List, Optional

from ..config import CONFIG, get_api_key
from ..errors import ErrorCode, VCException
from ..observability import log_event
from ..text_utils import tokenize


class EmbeddingProvider:
    name = "base"
    dim = 0

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]


class HashingEmbedding(EmbeddingProvider):
    """基于 token 哈希的确定性稀疏-稠密映射（signed hashing trick）。"""

    name = "hashing"

    def __init__(self, dim: int = None):
        self.dim = int(dim or CONFIG.hashing_dim)

    def _vec(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        counts = Counter(tokenize(text))
        if not counts:
            return vec
        for tok, n in counts.items():
            h = _md5(tok)
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if (h[4] & 1) else -1.0
            vec[idx] += sign * math.log1p(n)
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]


def _md5(s: str) -> bytes:
    import hashlib

    return hashlib.md5(s.encode("utf-8")).digest()


def pick_device(prefer: str = None) -> str:
    """选择推理设备：显式指定 > MPS(Apple Silicon) > CUDA > CPU。"""
    prefer = (prefer or CONFIG.embedding_device or "").strip()
    if prefer:
        return prefer
    try:
        import torch

        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # pragma: no cover - torch 可选
        pass
    return "cpu"


# HuggingFace 连不通时的国内镜像兜底（modelscope 已随环境安装）
_MODELSCOPE_ALIAS = {
    "BAAI/bge-small-zh-v1.5": "AI-ModelScope/bge-small-zh-v1.5",
    "BAAI/bge-base-zh-v1.5": "AI-ModelScope/bge-base-zh-v1.5",
    "BAAI/bge-reranker-base": "AI-ModelScope/bge-reranker-base",
}


def resolve_model_path(model_name: str) -> str:
    """本地目录优先；否则尝试 HF，再兜底 ModelScope 下载到本地目录。"""
    local = Path(model_name).expanduser()
    if local.exists():
        return str(local)

    cache_dir = Path(os.environ.get("VC_MODEL_CACHE", str(CONFIG.index_dir / "models")))
    cached = cache_dir / model_name.replace("/", "__")
    if cached.exists() and any(cached.iterdir()):
        return str(cached)

    try:
        from huggingface_hub import snapshot_download

        path = snapshot_download(repo_id=model_name, cache_dir=str(cache_dir))
        return path
    except Exception as exc:  # 网络不通或仓库不存在 -> 走 ModelScope
        log_event("model_download_fallback", model=model_name, reason=str(exc)[:120], via="modelscope")

    try:
        from modelscope import snapshot_download as ms_download

        for repo in (model_name, _MODELSCOPE_ALIAS.get(model_name, "")):
            if not repo:
                continue
            try:
                return ms_download(repo, cache_dir=str(cache_dir))
            except Exception:
                continue
    except Exception as exc:  # pragma: no cover
        raise VCException(ErrorCode.E_EMBED, "模型下载失败（HF 与 ModelScope 均不可用）: %s" % exc)
    raise VCException(ErrorCode.E_EMBED, "模型下载失败: %s" % model_name)


class BGEEmbedding(EmbeddingProvider):
    """本地 BGE 向量（sentence-transformers）。

    - 权重走 HF，失败自动兜底 ModelScope；
    - 向量做 L2 归一化（配合 Chroma cosine 空间）；
    - dim 由模型读出（bge-small-zh-v1.5 = 512），切换模型后 manifest 会判定为 full 重建。
    """

    name = "bge"

    def __init__(self, model_name: str = None, device: str = None, batch_size: int = None):
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:  # pragma: no cover - 可选依赖
            raise VCException(ErrorCode.E_EMBED, "未安装 sentence-transformers: %s" % exc)

        self.model_name = model_name or CONFIG.embedding_model
        self.batch_size = max(1, int(batch_size or CONFIG.embedding_batch_size))
        self.device = pick_device(device)
        path = resolve_model_path(self.model_name)
        self._model = SentenceTransformer(path, device=self.device)
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)
        if not self.dim:
            raise VCException(ErrorCode.E_EMBED, "无法获取模型维度: %s" % self.model_name)
        log_event("embedding_ready", provider="bge", model=self.model_name, dim=self.dim,
                  device=self.device, batch_size=self.batch_size)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vecs = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [list(map(float, v)) for v in vecs]


class OpenAICompatEmbedding(EmbeddingProvider):
    name = "openai_compat"

    def __init__(self, model: str = None, base_url: str = None, api_key_env: str = "OPENAI_API_KEY"):
        import requests  # 延迟导入，避免无外网时拖慢启动

        self._requests = requests
        self.model = model or CONFIG.embedding_model
        self.base_url = (base_url or CONFIG.llm_base_url).rstrip("/")
        self.api_key = get_api_key(api_key_env)
        if not self.api_key:
            raise VCException(ErrorCode.E_EMBED, "缺少环境变量 %s" % api_key_env)
        self.dim = 1024  # 实际维度以首次返回为准

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        resp = self._requests.post(
            self.base_url + "/embeddings",
            headers={"Authorization": "Bearer %s" % self.api_key},
            json={"model": self.model, "input": texts},
            timeout=CONFIG.timeout_llm,
        )
        if resp.status_code == 429:
            raise VCException(ErrorCode.E_LLM_RATE_LIMIT, "embedding 限流")
        if resp.status_code >= 400:
            raise VCException(ErrorCode.E_EMBED, "embedding 服务返回 %s" % resp.status_code)
        data = resp.json()["data"]
        data.sort(key=lambda x: x.get("index", 0))
        vecs = [d["embedding"] for d in data]
        self.dim = len(vecs[0])
        return vecs


_CACHE: dict = {}


def get_embedding_provider(allow_fallback: bool = True) -> EmbeddingProvider:
    """工厂：按配置构造，失败时（可选）回落 Hashing 以保证链路可用。"""
    key = CONFIG.embedding_provider
    if key in _CACHE:
        return _CACHE[key]
    provider: Optional[EmbeddingProvider] = None
    try:
        if key == "bge":
            provider = BGEEmbedding()
        elif key == "openai_compat":
            provider = OpenAICompatEmbedding()
        else:
            provider = HashingEmbedding()
    except Exception:
        if not allow_fallback:
            raise
        provider = HashingEmbedding()
    _CACHE[key] = provider
    return provider
