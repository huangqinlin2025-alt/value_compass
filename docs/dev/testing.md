# 测试与评测

## 1. 单元测试

```bash
python3 -m pytest tests -q      # 55 项，秒级
```

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_retrieval.py` | 中文分词、BM25 排序、IDF 相关性、RRF 融合、Hashing Embedding 确定性、过滤抽取 |
| `tests/test_ingest.py` | 表格整块切分、元数据完整性、manifest 四种增量决策、原子写 |
| `tests/test_graph.py` | 意图路由、数值闸门（接受/拒绝/页码）、兜底非空与摘录、safe_node 容错与超时、MockLLM 抽取式、图可编译 |
| `tests/test_schema.py` | 契约容错：枚举别名/大小写、置信度越界、空引用、markdown 围栏、脏输出判定 |
| `tests/test_llm_contract.py` | `FakeLLM` 契约链路：合法 JSON / 围栏 / 修复重试 / 脏输出抛错 / Mock 不声明 JSON 支持；router 规则红线不被推翻、失败回落规则；gate 拦截编造数字与伪造页码、LLM 抓"结论越界"；generator 按白名单剥离编造引用、JSON 失败回落 |
| `tests/test_eval.py` | 评测**口径**：真幻觉必须判出来；兜底摘录（context 清空）、摘录截断产生的半截数字、问题自带年份三类不得误判为幻觉；no-corpus 模式 hit=诚实 |

设计原则：**单测不依赖真实 PDF 与索引**，全部用合成数据，保证秒级可跑。
契约类用例用 `LLM_PROVIDER=fake` 的 `FakeLLM` 注入脚本化响应，因此**没有 API Key 也能覆盖完整分支**。

## 2. 金标评测

```bash
python3 scripts/eval.py                          # 金标：跑全部 22 题
python3 scripts/eval.py --verbose                # 逐题输出
python3 scripts/eval.py --limit 5
python3 scripts/eval.py --dataset cfqa --limit 60      # CFQA 语料外问题（看是否诚实）
python3 scripts/eval.py --dataset hallucination        # 注入编造数字，验证闸门拦截 + retry
python3 scripts/eval.py --dataset custom --file x.json # 自定义用例

# 参数扫描（调参用，结论与阈值权衡表见 docs/dev/tuning.md）
python3 scripts/sweep.py --stage retrieval --limit 12 --csv docs/dev/sweep_retrieval.csv
python3 scripts/sweep.py --stage chunk    --limit 12 --restore-best   # 会重建索引，慢
```

三套数据集：

| 数据集 | 来源 | 考察点 |
| --- | --- | --- |
| `golden` | `tests/goldens/qa_set.json`（22 题） | 召回命中 / 引用 / 闸门 / 兜底 |
| `cfqa` | `scripts/fetch_cfqa.py`（CFQA，MIT） | **语料外问题是否诚实**（兜底/拒答率、幻觉率） |
| `hallucination` | `src/vc/eval/hallucination.py` | 伪造数字是否被 gate 拦截并触发 `retry_shrink` |

金标集：`tests/goldens/qa_set.json`，字段：

```jsonc
{
  "q": "公司本期营业收入是多少？",
  "expect_keywords": ["营业收入"],   // 命中判据：关键词必须出现在召回上下文中
  "type": "METRIC",                  // 期望意图
  "expect_refuse": false             // 越界题置 true
}
```

指标定义：

| 指标 | 定义 | 解读 |
| --- | --- | --- |
| 命中率 | 期望关键词出现在召回上下文中的比例 | 主要衡量**召回**能力 |
| 引用率 | 产出可溯源引用的比例 | 衡量引用抽取与校验是否工作 |
| 数值闸门通过 | 通过 `faithfulness_gate` 的比例 | 衡量防幻觉是否生效 |
| 幻觉率 | 答案中出现、召回原文中不存在的数字 | **越低越好**；出现即为 P0 缺陷 |
| 兜底率 | 走兜底链路的比例 | 过高=召回顾此失彼；过低=防线可能失效 |
| 诚实率 | CFQA 语料外问题走兜底/拒答的比例 | 越高越"不硬答" |
| 平均耗时 / P95 | 端到端单题耗时 | 默认 Mock，真实瓶颈在 LLM |

## 3. 当前基线（2026-09-20，118 页半年报，331 chunk）

**金标 22 题（BGE 512 维 + MockLLM）：**

```
样本数        : 22
命中率        : 95.5%
引用率        : 86.4%
数值闸门通过  : 86.4%
幻觉率        : 0.0%
兜底率        : 9.1%
平均耗时      : 0.163s | P95 0.061s
```

**CFQA 语料外 60 题（考察"不硬答"，`min_top1_relevance=0.28` 默认档）：**

```
样本数        : 60
命中率(诚实率): 43.3%
引用率        : 56.7%
数值闸门通过  : 56.7%
幻觉率        : 0.0%
兜底率        : 43.3%
平均耗时      : 0.080s | P95 0.052s
```

> 耗时受机器负载影响有 ±20% 波动，重点看量级（百毫秒级，瓶颈在向量召回而非生成）。

> 该数据集问的是**语料外**公司，命中率即"诚实率"（越高越不硬答）。
> 阈值提到 `0.34` 时诚实率升到 **50.0%**，但金标引用率/闸门会从 86.4% 掉到 77.3%——
> 这是一对明确的权衡，完整扫描数据见 `docs/dev/tuning.md`。

**幻觉探针：** 注入 `8888.88 亿元` / `[P99]` 后，`faithfulness_gate` 拦截、`retry_count=1`、
最终转兜底，伪造内容**不出现在最终答案**中（`python3 scripts/eval.py --dataset hallucination`）。

说明：该基线在 **本地 BGE + MockLLM** 下取得——向量已是真语义模型，
但答案仍由抽取式 Mock 生成；接入真 LLM（`LLM_PROVIDER=openai_compat`）后需重测并更新本表。

> 阈值标定过程（可复现）：相关性闸门先后用过三种口径，效果差异很大——
> 字 bigram 覆盖率（几乎无法区分）→ IDF 加权（前缀污染、过渡词稀释）→
> **只统计语料中真实存在的词的 IDF 加权覆盖率**（当前口径，22 题兜底率从 27.3% 降到 4.5%）。

## 4. 故障注入自测

见 `docs/design/resilience.md` 第 7 节。建议每次改动容错逻辑后跑一遍：

```bash
VECTOR_STORE_PROVIDER=pickle python3 scripts/query.py "营业收入是多少？"
TIMEOUT_LLM=0.001 python3 scripts/query.py "营业收入是多少？" --trace
RERANK_PROVIDER=none python3 scripts/query.py "营业收入是多少？"
```

## 5. 新增用例的规范

- 合成数据优先，避免依赖真实 PDF；
- 每个新增节点至少 2 条：正常路径 + 失败被捕获；
- 金标集新增题目必须标注 `type`，并保证报告里确实存在该信息（否则会拉低命中率且掩盖真实问题）。
