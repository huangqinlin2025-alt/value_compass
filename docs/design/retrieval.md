# 知识检索细则

## 1. 分块策略

财报的核心信息在表格里：**表格一旦被按字数切碎，"12.34" 就不知道属于哪一行**。

| 内容类型 | 策略 | 参数 |
| --- | --- | --- |
| 表格区 | 整块成 chunk；超长按行切分并**重复表头行** | `max_table_chars = max(chunk_size*2, 1600)` |
| 正文 | 滑窗切分 | `chunk_size=800`、`overlap=120`（约 15%，防跨段切断数字） |
| 残块 | `<120` 字并入同页相邻块 | `min_chunk_chars=120` |

表格区判定（满足任一即视为表格行）：

- 一行中数字字面量 ≥ 2 个；
- 一行含表头词（项目 / 本期 / 上期 / 同比 / 增减 / 期末 / 年初 / 金额 / 占比 / 合计 / 附注）且至少有 1 个数字。

章节标题判定（`src/vc/ingestion/metadata.py`）：

- `第X节 xxx`、`一、xxx`、`（一）xxx`、`1. xxx`；
- 已知章节名前缀（重要提示 / 公司基本情况 / 主要财务数据 / 管理层讨论与分析 / 财务报告 …）；
- 标题栈最多保留 4 层，生成 `section_path = "A > B > C"`。

每个 chunk 文本前置 `[P页码｜章节路径]`，作用有三：提升 BM25 命中、让向量带上位置语义、便于答案标注来源。
**注意**：做相关性/覆盖率计算时必须用 `strip_page_prefix()` 去掉该前缀，否则章节名里的字会被算作命中。

## 2. 元数据 Schema

```jsonc
{
  "chunk_id": "4b5984b3af9e83fe-00123",  // {doc_id}-{序号}
  "doc_id": "4b5984b3af9e83fe",          // 文件路径 hash，前 16 位
  "doc_version": "4b5984b3af9e83fe@202609181430",
  "company": "中国国际贸易中心股份有限公司",
  "short_name": "中国国贸",              // 证券简称，用于"茅台/宁德时代"这类口语命中
  "stock_code": "600007",
  "report_period": "2026H1",             // H1 / A / Q1 / Q3
  "report_type": "半年报",
  "industry": "商贸零售",                 // 行业分类：跨公司/跨行业对比的过滤维度
  "page": 12,
  "section_path": "管理层讨论与分析 > 报告期内主要经营情况",
  "table_flag": true,
  "text": "[P12|...]\n营业收入 1,815,972,101 ...",
  "content_hash": "…",
  "source_path": "./data/sample_report.pdf",
  "ingest_time": "2026-09-18 14:30:00",
  "embedding_provider": "hashing",
  "embed_dim": 384
}
```

入库 Chroma 时只保留 `str/int/float/bool` 字段（见 `_safe_meta`），且不接受 `None`。

## 3. 三路召回

| 路 | 实现 | 保什么 | top_k | 过滤 |
| --- | --- | --- | --- | --- |
| BM25 | `rank_bm25.BM25Okapi`（缺失时回落内置 `_SimpleBM25`） | 数字、会计科目、专有名词**字面命中** | `max(route.top_k*3, 20)` | 后置元数据过滤 |
| 向量 | Chroma（降级 PickleStore） | 同义泛化 | `max(20, route.top_k*3)` | Chroma `where` |
| 元数据 | BM25 候选 + 元数据过滤 + 覆盖度排序 | 范围正确（公司 / 期间 / 行业） | 10 | `stock_code` / `report_period` / `industry` |

过滤条件抽取（`retrieval/filters.py`）：

- `stock_code`：6 位数字，或命中已知公司名/简称（多文档语料下逐家比对 `known.docs`）；
- `report_period`：`2026年半年度 → 2026H1`、`年度报告 → A`、季度 → `Q1/Q3`；
- `industry`：行业触发词命中（白酒/银行/保险/新能源/光伏/电子/食品/汽车/医药），
  且只在 `known.industries` 里存在的行业才过滤，避免抽到库里没有的维度；
- `section_keywords`：财务 / 现金流 / 资产负债 / 利润 / 股东 / 治理 / 风险 / 审计；
- `prefer_table`：出现"同比/占比/率/表/明细/构成"等词。

两个多文档场景下的硬约束：

- **元数据路必须"过滤优先"**（`BM25Index.search_where`）：先按元数据圈定范围再排序。
  先取 BM25 top-N 再后置过滤会整路为空——公司名/证券简称常出现在页眉而被
  `clean_normalize` 当噪声清掉，"宁德时代净利润"的 top-N 里可能一条宁德时代都没有。
- **Chroma 多条件 `where` 必须显式 `$and`**：`{"stock_code":..,"report_period":..}` 这种
  隐式多键 AND 会被拒绝（`Expected where to have exactly one operator`），
  而"某公司某年财报"正好会同时抽到两个条件。
  BM25 路则做**软过滤**：过滤后为空就退回不过滤结果，宁可宽松也不能打空召回。

**抽不到就不强行过滤**——误过滤比不过滤伤害更大。

## 4. 融合：加权 RRF

```
score(d) = Σ_i  w_i / (k + rank_i(d))        k = 60
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `w_bm25` | 1.0 | METRIC/CALC 意图下提到 1.3 |
| `w_vector` | 1.0 | QUALITATIVE/SUMMARY 意图下提到 1.3 |
| `w_meta` | 0.6 | 只作范围约束，权重最低 |
| `table_boost` | 1.15 | 命中 `table_flag` 乘性加成 |

为什么用 RRF 而不是分数相加：三路分数尺度完全不同（BM25 未归一化、向量余弦、覆盖度 0~1），
直接相加会被量纲大的一路主导；RRF 只用排名，天然免疫。

## 5. 相关性闸门（防"硬答"）

融合后追加一道闸门，避免"召回到了不相关的东西还照答"：

```
relevance = Σ_{t ∈ q_known ∩ d} idf(t) / Σ_{t ∈ q_known} idf(t)
q_known = 问句中「在语料里真实存在（df>0）」且长度 ≥ 2 的词
取 fused 前 5 个候选中最好的那个（RRF 第一名未必字面最相关）
```

- `idf(t) = ln(1 + (N - df + 0.5)/(df + 0.5))`；
- 阈值 `MIN_TOP1_RELEVANCE = 0.28`；实测（118 页半年报）：语料中无此词 → 0.0，相关问题 ≥ 0.30；
- 低于阈值 → 记 `E_LOW_SCORE` → 转兜底，**不做无依据作答**。

> 三个踩过坑才改对的点：
> 1. 早期用"字 bigram 覆盖率"，在 800 字长块上基线就有 0.15~0.25，几乎无法区分相关与否；
> 2. 改成 IDF 加权后，章节前缀 `[P12|财务报告]` 里的"财报"会被算作命中，必须 `strip_page_prefix`；
> 3. 问句里的过渡 bigram（"入是/是多/多少"）在语料里 `df=0`，会被算成最大 IDF 且永远命中不了，
>    把相关性压到 0.1 以下 → 现在只统计 `df>0` 的词；若一个都不存在，直接判 0（超出知识库范围）。

**已知边界**：该闸门能可靠拦住「问题里的词知识库中根本没有」和「覆盖率过低」两类；
对"用词通用但主题无关"的问句（如"量子计算对本财报的影响"，靠"影响"二字拿到 0.38）区分能力有限。
这类情况最终由 `faithfulness_gate` + `output_guard`（无来源不得出数字）兜住安全性，
质量层面则需靠更强的 embedding 与 rerank 解决。

## 6. 重排与压缩

两档重排，`RERANK_PROVIDER` 切换：

| 档位 | 实现 | 适用 | 成本 |
| --- | --- | --- | --- |
| `heuristic`（默认） | 0.45*覆盖率 + 0.25*数值命中 + 0.15*表格加权 + 0.15*页码邻近 + 3.0*RRF分 | 零依赖、毫秒级 | 无 |
| `bge` | `CrossEncoderReranker`（`BAAI/bge-reranker-base`，逐对打分） | 召回噪声大、需要语义精排 | 需 torch，仅对 top-N 打分 |
| `none` | `NoOpReranker` | 只想看融合顺序 | 无 |

- `prefer_table` 的意图会先把 `table_flag` 的块提到前面；
- 真模型权重缺失 / torch 不可用 → 工厂回落启发式，记 `degraded=["rerank:fallback_heuristic"]`；
- 节点失败/超时 → 直接用 `fused` 顺序，记 `degraded=["rerank"]`；
- 重排结果携带 `rerank_score` 与逐路名次 `rank_by_route`，供 UI 解释"为什么是它"。

上下文压缩：按分数降序填入 `CONTEXT_TOKEN_BUDGET=4000`（中文按 0.6 token/字粗估），按 `chunk_id` 去重。

## 7. 当前基线与已知限制

- **`EMBEDDING_PROVIDER=auto`：装了 sentence-transformers 即自动用本地 BGE**（`bge-small-zh-v1.5`，512 维，MPS/CPU 自动选设备），
  未安装则回落 `HashingEmbedding`；切档后必须 `python3 scripts/ingest.py --force`
  （维度变化被 `plan_diff` 识别为 `full`，且 Chroma 集合名带 `provider_dim` 后缀，天然隔离新旧空间）；
- 换 embedding 后可用 `scripts/ingest.py` 末尾的**落盘校验**确认 chroma / bm25.pkl / manifest / 快照四者一致；
- 实测金标集（22 题，BGE + MockLLM）：命中率 95.5%、引用率 86.4%、数值闸门通过 86.4%、幻觉率 0%、兜底率 9.1%（见 `docs/dev/testing.md`）；
- 未命中样本：问"净利润同比增长率"时召回的是数据表行，答案里没有"增长"二字——
  属于"答案表述"问题而非召回缺失，真 LLM 接入后由 generator 改写解决；
- 参数敏感度：检索期参数（top_k / 相关性阈值）在 22 题上差异很小，切分参数差异明显
  （800/120 优于 1100/1400），详见 `docs/dev/tuning.md`。
