# ADR-0003：Embedding 与 LLM 走可插拔适配器，默认零依赖实现

- 状态：accepted
- 日期：2026-09-18
- 相关：`docs/dev/how-to-add-provider.md`

## 背景

需求明确要求"先不接真模型，用 Mock/规则桩把架构跑通，模型节点留适配器，后续随时替换"，
同时又要求"Chroma + BM25 + BGE 向量混合"。二者存在冲突：BGE 需要下载 100MB+ 权重，弱网环境可能失败，
会让"零密钥零外网可跑通"的目标落空。

## 备选方案

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| 直接接 BGE + 真 LLM | 效果好 | 无 Key/无网就跑不起来，架构无法验证 |
| 只写接口不写实现 | 干净 | 无法端到端运行与评测 |
| **适配器 + 零依赖默认实现** | 端到端可跑；切换只改环境变量 | 默认效果弱，需明确标注为"下限基线" |

## 决策

1. 定义 `EmbeddingProvider` / `LLMProvider` / `Reranker` / `VectorStore` 四个接口；
2. 默认实现零依赖：`HashingEmbedding`（md5 signed hashing，跨进程**确定性**）、
   `MockLLM`（抽取式作答，答案中每个数字都来自原文）、`HeuristicReranker`；
3. 可选实现：`BGEEmbedding`、`OpenAICompatEmbedding`、`OpenAICompatLLM`，
   **延迟导入**依赖，构造失败自动回落默认实现并写入 `degraded`；
4. 切换只改环境变量（`EMBEDDING_PROVIDER` / `LLM_PROVIDER`），图结构零改动。

## 补充决策：MockLLM 不二次排序

初版 MockLLM 自己再按覆盖度排序挑选片段，把上游 RRF + rerank 的成果抵消了
（检索正确但答案用了错误片段）。现改为**严格尊重上游顺序，只在每个片段内抽最佳句子**。

## 后果

- 正面：任意环境可端到端验证；故障注入与容错逻辑可完整测试；
- 负面：默认检索质量受限于伪向量语义（金标命中率 95.5% 已接近关键词检索的天花板，
  但同义泛化如"增长率↔同比减少"仍未覆盖），属"架构跑通"的下限基线，
  换 BGE 后须重新评测并更新 `docs/dev/testing.md` 的基线；
- 义务：换 embedding provider 后必须 `scripts/ingest.py --force` 重建索引。
