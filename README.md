# value_compass · 金融垂类 RAG 知识库问答助手

基于 **LangGraph** 编排的财报问答助手。当前语料：

- `data/sample_report.pdf`（中国国际贸易中心股份有限公司 600007 · 2026 年半年度报告 · 118 页）
- `data/reports/`（10 家 A 股 × 2023-2025 年报/半年报共 60 份，源自巨潮资讯网官方披露件；
  每份 PDF 配同名 `*.meta.json` 存证券简称/代码/报告期/行业）

设计特点：**三路并行召回 + 加权 RRF 融合**、**意图路由（规则 + LLM JSON）**、
**数值一致性闸门（正则底线 + LLM 判定，防幻觉）**、**CrossEncoder 重排**、
**逐节点容错隔离与降级链**、**五类兜底话术**。

- 默认 `hashing/mock/heuristic` 三档零依赖，**不联网、不需要 Key** 即可跑通；
- 装了 `sentence-transformers` 后自动切 **本地 BGE（`bge-small-zh-v1.5`，512 维）**；
- 有 API Key 后切 `openai_compat` 即可拿到结构化答案与引用，**零改代码**。

---

## 一、快速开始

```bash
# 1) 安装依赖（可选段：真实向量/重排模型，装不上也能跑）
python3 -m pip install -r requirements.txt

# 2) 入库（末尾会打印"落盘校验"表）
python3 scripts/ingest.py                 # 单文档增量；换模型/改切分用 --force 全量重建
EMBEDDING_PROVIDER=bge python3 scripts/ingest.py --force

# 2') 财报语料：抓取 + 批量入库
python3 scripts/crawl_reports.py --dry-run          # 先看命中哪些报告
python3 scripts/crawl_reports.py                    # 抓年报/半年报 PDF + 同名元数据
python3 scripts/ingest_corpus.py --dir data/reports # 批量入库（BM25 末尾统一重建）

# 3) 问答
python3 scripts/query.py "公司本期营业收入是多少？"
python3 scripts/query.py "营业收入是多少？" --trace     # 看逐节点耗时/错误码/降级
python3 scripts/query.py                                # 交互式多轮

# 4) 评测（三套数据集）
python3 scripts/eval.py                        # 金标 22 题
python3 scripts/eval.py --dataset cfqa --limit 60   # 语料外问题是否"诚实"
python3 scripts/eval.py --dataset hallucination     # 编造数字是否被闸门拦截

# 5) 界面与服务
python3 -m streamlit run apps/streamlit_app.py        # 知识星图 + trace 时间线 + 引用角标
python3 -m uvicorn apps.api:app --reload --port 8000  # /ask /config /stats
```

输出示例：

```
根据《中国国际贸易中心股份有限公司》：
- 报告期内，公司实现营业收入 18.2 亿元，同比减少 3.90% [P10]

来源：P10、P12

本回答基于公开财报文本自动提取，不构成投资建议。
```

> 默认使用 `MockLLM` + `HashingEmbedding`（零依赖），**不联网、不需要 Key**。
> 换真模型只需环境变量，见 `docs/dev/setup.md`；参数怎么定的见 `docs/dev/tuning.md`。

---

## 二、架构一图流

```
[data/*.pdf]
  -> purge_stale -> load_pdf -> clean_normalize -> enrich_metadata
  -> table_aware_split -> dedup_hash -> plan_diff -+-> skip_node
                                                   +-> embed_upsert -> build_bm25 -> write_manifest
                             |
              +--------------+---------------+
              v              v               v
       [Chroma 向量库]   [bm25.pkl]    [manifest.json]
              |              |               |
[问题] -> query_rewrite -> intent_router -+-> OOS/CHITCHAT/UNCLEAR -> 短路输出
                                          +-> Send 并行 ┌ bm25_recall ┐
                                                        ├ vector_recall┤ -> rrf_fusion
                                                        └ meta_recall  ┘      |
                     fallback <---(空/低分/低相关)---------+                    v
                        ^                                        rerank -> compress -> generator
                        |                                                            |
                        +---(数值不通过 且 已重试)------ faithfulness_gate <----------+
                                                              |通过
                                              citation_validate -> output_guard -> 答案+引用+trace
```

完整 Mermaid 图与节点规格见 `docs/design/architecture.md`、`docs/design/nodes.md`。

---

## 三、文档地图

| 我想… | 看这里 |
| --- | --- |
| 看完整架构图与节点规格 | `docs/design/architecture.md`、`docs/design/nodes.md` |
| 了解分块 / 元数据 / 三路召回 / RRF / 相关性闸门 | `docs/design/retrieval.md` |
| 了解增量更新 / manifest / 原子替换 / 回滚 | `docs/design/knowledge_update.md` |
| 了解意图体系与路由降级 | `docs/design/routing.md` |
| 了解错误码 / 重试 / 熔断 | `docs/design/error_handling.md` |
| 了解容错隔离与降级链 | `docs/design/resilience.md` |
| 了解五类兜底话术 | `docs/design/fallback.md` |
| 搭环境 / 跑起来 | `docs/dev/setup.md` |
| 加节点 / 加 Provider | `docs/dev/how-to-add-node.md`、`how-to-add-provider.md` |
| 跑测试与评测 | `docs/dev/testing.md` |
| 参数是怎么调出来的 | `docs/dev/tuning.md` |
| 排查问题 | `docs/dev/observability.md` |
| 接口对接 | `docs/api/rest_api.md` |
| 为什么这么设计 | `docs/adrs/`（5 篇决策记录） |

---

## 四、目录结构

```
src/vc/          核心：graph / nodes / retrieval / ingestion / providers / eval
                 schema.py + prompts/  = LLM 结构化输出契约层
apps/            FastAPI 服务 + Streamlit Demo
scripts/         CLI：ingest / ingest_corpus / crawl_reports / query / eval / sweep / fetch_cfqa
tests/           pytest 单测 + goldens 金标集 / CFQA
docs/            design / dev / api / adrs
data/            语料 PDF（sample_report.pdf + reports/ 财报语料）+ data/cfqa 评测集
index/           运行产物（chroma / bm25.pkl / manifest.json / snapshots）
```

---

## 五、当前能力基线（2026-09-20）

**金标 22 题（本地 BGE 512 维 + MockLLM）：**

```
# 单文档（data/sample_report.pdf，331 chunk）
命中率 95.5% ｜ 引用率 86.4% ｜ 闸门通过 86.4% ｜ 幻觉率 0.0% ｜ 兜底率 9.1% ｜ P95 0.061s

# 加挂财报语料后（61 份文档，56755 chunk）——同一套 22 题
命中率 100.0% ｜ 引用率 81.8% ｜ 闸门通过 81.8% ｜ 幻觉率 0.0% ｜ 兜底率 13.6% ｜ P95 1.397s
```

语料放大 170 倍后命中率反而升、幻觉率仍为 0；代价是 P95 从 0.06s 涨到 1.4s
（BM25 打分与向量检索都要扫 5.7 万 chunk），首问另需约 10s 冷启动预热。

**CFQA 语料外 60 题（考察"不硬答"）：** 诚实率 43.3% ｜ 幻觉率 0.0% ｜ 引用率 56.7%

**幻觉探针：** 注入 `8888.88 亿元` / `[P99]` → 闸门拦截、`retry_count=1`、转兜底，伪造内容不出现在最终答案。

说明：向量已是真语义模型，但答案仍由抽取式 Mock 生成；
接真 LLM（`LLM_PROVIDER=openai_compat`）后需重测并更新 `docs/dev/testing.md`。
调参过程与阈值权衡见 `docs/dev/tuning.md`。

---

## 六、安全与合规

- API Key 仅从环境变量读取，禁止硬编码；`.env` 不得提交（见 `.env.example`）；
- `/ingest` 默认关闭，开启需 `VC_INGEST_TOKEN` + 请求头 `X-Token`；
- 日志禁止打印密钥与正文全文；
- 所有答案统一追加"不构成投资建议"；越界问题（买卖/荐股/目标价）直接拒答。
