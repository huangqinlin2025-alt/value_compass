"""可插拔 Provider：LLM / Embedding / Reranker。

切换实现只改环境变量（LLM_PROVIDER / EMBEDDING_PROVIDER / RERANK_PROVIDER），
图结构零改动。新增实现必须同步 docs/adrs/ 与 docs/dev/how-to-add-provider.md。
"""
from .embedding import EmbeddingProvider, get_embedding_provider
from .fake_llm import FakeLLM
from .llm import LLMProvider, get_llm_provider
from .reranker import Reranker, get_reranker

__all__ = [
    "EmbeddingProvider",
    "get_embedding_provider",
    "LLMProvider",
    "FakeLLM",
    "get_llm_provider",
    "Reranker",
    "get_reranker",
]
