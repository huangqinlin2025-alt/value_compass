# 环境搭建

## 1. 依赖安装

```bash
python3 -m pip install -r requirements.txt
```

> Python 3.9 注意：`streamlit` 需锁 `>=1.39,<1.40`（更高版本要求 Python ≥3.10）。

## 2. 可选依赖

| 用途 | 安装（Python 3.9 实测可用组合） | 启用方式 |
| --- | --- | --- |
| 真向量语义（BGE） | `pip install "torch==2.4.1" "sentence-transformers==2.7.0" "transformers==4.44.2" "huggingface-hub==0.24.7"` | `EMBEDDING_PROVIDER=bge`（默认 `auto`：装了就自动用） |
| 真重排模型 | 同上（CrossEncoder） | `RERANK_PROVIDER=bge`（默认 `heuristic`） |
| 真 LLM | 无需额外包（用 `requests`） | `LLM_PROVIDER=openai_compat` + Key |
| 契约测试（无 Key） | 无需安装 | `LLM_PROVIDER=fake` |

> 权重默认从 HuggingFace 下载（`BAAI/bge-small-zh-v1.5`，约 400MB；`BAAI/bge-reranker-base` 约 1.1GB）；
> HF 不可达时自动转 ModelScope，缓存目录可用 `VC_MODEL_CACHE` 指定。

## 3. 环境变量

复制 `.env.example` 为 `.env` 并按需填写（**`.env` 不得提交**）：

```bash
cp .env.example .env
```

核心变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EMBEDDING_PROVIDER` | `auto` | `auto` / `hashing` / `bge` / `openai_compat`（auto=有 sentence-transformers 用 bge） |
| `EMBEDDING_BATCH_SIZE` | `32` | BGE 编码批大小 |
| `EMBEDDING_DEVICE` | 空 | 空=自动选 mps/cuda/cpu |
| `RERANK_PROVIDER` | `heuristic` | `heuristic` / `bge` / `none` |
| `RERANK_MODEL` | `BAAI/bge-reranker-base` | CrossEncoder 模型 |
| `LLM_PROVIDER` | `mock` | `mock` / `fake`（契约测试）/ `openai_compat` |
| `LLM_JSON_MODE` | `true` | 是否下发 `response_format=json_object` |
| `LLM_JSON_REPAIR` | `1` | Schema 校验失败后的修复重试次数 |
| `ROUTER_LLM_ENABLED` | `true` | 规则犹豫时是否问 LLM |
| `GATE_LLM_ENABLED` | `true` | 数值闸门是否叠加 LLM 语义判定 |
| `GENERATOR_JSON_ENABLED` | `true` | generator 是否走 `AnswerResult` JSON |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容地址 |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `OPENAI_API_KEY` | — | 仅从环境变量读取 |
| `VECTOR_STORE_PROVIDER` | `chroma` | `chroma` / `pickle` |
| `VC_INGEST_TOKEN` | 空 | 为空时关闭 `/ingest` 接口 |

完整列表见 `src/vc/config.py`。

## 4. 首次跑通

```bash
python3 scripts/ingest.py                       # 入库：118 页 -> 331 chunk（末尾自动做落盘校验）
python3 scripts/query.py "公司本期营业收入是多少？"
python3 scripts/query.py                        # 交互式多轮
python3 scripts/query.py "营业收入是多少？" --trace   # 看节点耗时
python3 scripts/eval.py                         # 22 题金标基线
python3 scripts/eval.py --dataset hallucination # 注入编造数字，验证闸门拦截
```

入库成功后 CLI 会打印**落盘校验**（manifest / chroma / bm25.pkl / 快照四者 chunk 数一致、
provider 与 dim 一致）；任一项 ✘ 都说明索引不完整，必须 `--force` 重建。

## 5. 启动服务与界面

```bash
# API
python3 -m uvicorn apps.api:app --reload --port 8000
curl -X POST http://127.0.0.1:8000/ask -H 'Content-Type: application/json' \
     -d '{"question":"公司本期营业收入是多少？"}'

# Streamlit
python3 -m streamlit run apps/streamlit_app.py
```

## 6. 目录约定

```
src/vc/        核心代码（graph / nodes / retrieval / ingestion / providers）
apps/          FastAPI 与 Streamlit 入口
scripts/       CLI：ingest / query / eval
tests/         pytest 单测 + goldens 金标集
docs/          design / dev / api / adrs
index/         运行产物：chroma 库、bm25.pkl、manifest.json、snapshots（不提交）
data/          语料（PDF 体积大，按需提交或用 git-lfs）
```

## 7. 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `No module named 'langgraph'` | 未安装 | `pip install langgraph` |
| `streamlit` 装不上 | Python 3.9 版本上限 | `pip install "streamlit<1.40"` |
| chromadb 初始化失败 | 编译/权限问题 | `VECTOR_STORE_PROVIDER=pickle` |
| 换 embedding 后召回变差 | 旧索引维度不匹配 | `python3 scripts/ingest.py --force`（集合名带 `provider_dim` 后缀，旧数据仍在，可回退） |
| BGE 权重下载不动 | HF 不可达 | 自动转 ModelScope；或设 `VC_MODEL_CACHE` 指定缓存/本地目录 |
| `CrossEncoder` 加载很慢 | 首次需下载 1.1GB 权重 | 等一次即可，之后走本地缓存；不想等就用 `RERANK_PROVIDER=heuristic` |
| 问答总是 `degraded: rerank:fallback_heuristic` | 装了 `RERANK_PROVIDER=bge` 但 torch/权重不可用 | 属预期降级；查看 `GET /config` 确认实际档位 |
| 所有问题都走兜底 | 相关性阈值过严 | 调低 `MIN_TOP1_RELEVANCE` |
| 入库后问答仍说"未检索到" | BM25/向量库未就绪 | 检查 `index/manifest.json` 与 `index/bm25.pkl` |
