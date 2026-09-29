# 如何新增一个 Provider

Provider 是**可插拔适配层**：切换实现只改环境变量，图结构零改动。
目前有三类：`LLMProvider` / `EmbeddingProvider` / `Reranker`，外加 `VectorStore`。

## 1. 接口契约

```python
# src/vc/providers/embedding.py
class EmbeddingProvider:
    name = "your_provider"
    dim = 768
    def embed_documents(self, texts: List[str]) -> List[List[float]]: ...
    def embed_query(self, text: str) -> List[float]: ...

# src/vc/providers/llm.py
class LLMProvider:
    name = "your_provider"
    def generate(self, prompt: str, *, timeout: float = None) -> str: ...
    def answer(self, query, chunks, intent="", *, timeout=None) -> str: ...

# src/vc/providers/reranker.py
class Reranker:
    name = "your_provider"
    def rerank(self, query: str, chunks: List[dict], top_k: int = 5) -> List[dict]: ...

# src/vc/retrieval/vectorstore.py
class VectorStore:
    name = "your_store"
    def upsert(self, chunks, vectors) -> int: ...
    def query(self, vector, top_k=20, where=None) -> List[dict]: ...
    def delete_by_doc(self, doc_id: str) -> int: ...
    def delete_where(self, where: dict) -> int: ...
    def count(self) -> int: ...
```

## 2. 实现要点

- **延迟导入**：`import requests` / `sentence_transformers` 等放在 `__init__` 内，
  避免无外网或未安装时拖慢启动；
- **失败要抛 `VCException` 并带正确 `ErrorCode`**（`E_LLM_TIMEOUT` / `E_LLM_RATE_LIMIT` / `E_EMBED` …），
  `safe_node` 才能正确判断是否重试；
- **密钥只从环境变量读**：使用 `config.get_api_key(env_name)`，禁止硬编码、禁止写入日志；
- **统一在工厂函数里注册**，并保留 fallback：

```python
def get_embedding_provider(allow_fallback: bool = True) -> EmbeddingProvider:
    try:
        if key == "your_provider":
            provider = YourEmbedding()
        ...
    except Exception:
        if not allow_fallback:
            raise
        provider = HashingEmbedding()   # 兜底
```

## 3. 切换方式

```bash
EMBEDDING_PROVIDER=bge python3 scripts/ingest.py --force   # 维度变化必须强制重建
LLM_PROVIDER=openai_compat LLM_BASE_URL=... OPENAI_API_KEY=... python3 scripts/query.py "…"
VECTOR_STORE_PROVIDER=pickle python3 scripts/query.py "…"
```

> **Embedding provider 变化会被 `plan_diff` 自动识别为 `full` 全量重建**，
> 但仍建议显式 `--force`，避免同一 provider 换模型（维度不变）时漏重建。

## 4. 文档义务（硬性）

新增 Provider 属于架构决策，必须：

1. 新增 `docs/adrs/ADR-XXXX-采用-XX-作为-YY-provider.md`（背景 / 选项 / 决策 / 后果）；
2. 更新 `docs/dev/setup.md` 的可选依赖与环境变量表；
3. 更新 `docs/design/resilience.md` 的组件级降级矩阵；
4. 在 `CHANGELOG.md` 追加条目。

## 5. 自测清单

- [ ] 无网络 / 无 Key 时，工厂函数能回落到兜底实现，链路仍可跑通；
- [ ] 超时与限流分别抛出正确的 `ErrorCode`，且能被重试；
- [ ] 连续失败 3 次后该 provider 被熔断（`/ask` 返回 `degraded` 含 `circuit_open`）；
- [ ] 日志中不出现 Key、Authorization 头与正文全文。
