# ADR-0004：Chroma 降级方案与索引快照同源

- 状态：accepted
- 日期：2026-09-18
- 相关：`docs/design/knowledge_update.md`、`docs/design/resilience.md`

## 背景

两个风险：

1. `chromadb` 在 Python 3.9 / macOS 上可能安装或初始化失败（依赖较重、需编译）；
2. BM25 与向量库若基于不同时刻、不同内容的 chunk 构建，同一 `chunk_id` 在两路内容不一致，
   会导致答案引用的页码与原文对不上。

## 备选方案

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| 强制依赖 Chroma | 生产级 | 环境不兼容即全盘不可用 |
| 只用纯 Python 自研向量库 | 零依赖 | 无持久化优化、无 ANN，规模大了慢 |
| **双实现 + 接口一致 + 自动降级** | 任何环境可用；后续可无缝切回 | 需维护两套实现 |

## 决策

1. `VectorStore` 接口下实现 `ChromaStore`（默认）与 `PickleStore`（内存 dict + 纯 Python 余弦 + pickle 落盘）；
2. 工厂 `get_vector_store(allow_fallback=True)`：主实现初始化失败自动降级，上层无感；
   可通过 `VECTOR_STORE_PROVIDER=pickle` 显式指定；
3. **索引快照同源**：入库时把 chunk 列表按 `doc_id` 落盘到 `index/snapshots/{doc_id}.pkl`；
   `build_bm25` 读取**所有 active 文档的快照**重建倒排，向量库的写入也来自同一批 chunk；
4. `write_manifest` 在写盘前校验 `store.count() > 0`，不通过则抛错保留旧版本。

## 后果

- 正面：环境兼容性风险被隔离在适配层；两路召回内容严格一致，引用可信；
- 负面：`PickleStore` 在万级以上 chunk 时检索为 O(N) 线性扫描，仅作降级兜底，不作生产默认；
- 快照落盘带来额外磁盘占用（331 chunk 约几 MB），可接受。
