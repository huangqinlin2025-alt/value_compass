# REST API

启动：

```bash
python3 -m uvicorn apps.api:app --reload --port 8000
# 或 python3 apps/api.py
```

Swagger：`http://127.0.0.1:8000/docs`

---

## GET /health

```bash
curl http://127.0.0.1:8000/health
```

```json
{"status": "ok", "docs": 1, "chunks": 331}
```

## GET /stats

```jsonc
{
  "docs": [{"company": "中国国际贸易中心股份有限公司", "stock_code": "600007",
            "report_period": "2026H1", "chunk_count": 331,
            "doc_version": "4b5984b3af9e83fe@202609201528", "status": "active"}],
  "known_meta": {"company": "...", "stock_code": "600007", "report_period": "2026H1"},
  "vector_store": "chroma",
  "embedding_provider": "bge",
  "llm_provider": "mock",
  "rerank_provider": "heuristic",
  "index_verify": {                 // 落盘一致性校验，见下
    "ok": true, "docs": 1, "chunks": 331, "provider": "bge", "dim": 512,
    "checks": [{"name": "chroma_chunks", "expect": 331, "actual": 331, "ok": true},
               {"name": "bm25_chunks", "expect": 331, "actual": 331, "ok": true},
               {"name": "snapshot_chunks", "expect": 331, "actual": 331, "ok": true},
               {"name": "embedding_provider", "expect": "bge", "actual": "bge", "ok": true},
               {"name": "embed_dim", "expect": 512, "actual": 512, "ok": true}]
  }
}
```

## GET /config

只读配置快照 + 落盘校验。排障时**先看这个接口**：确认线上跑的是不是"你以为的那套模型和参数"。

```jsonc
{
  "providers": {"embedding": "bge", "llm": "mock", "rerank": "heuristic", "vector_store": "chroma"},
  "retrieval": {"top_k_bm25": 20, "top_k_vector": 20, "top_k_meta": 10,
                "top_k_final": 5, "min_top1_relevance": 0.28, "rrf_k": 60},
  "contracts": {"router_llm_enabled": true, "gate_llm_enabled": true,
                "generator_json_enabled": true, "llm_json_mode": true, "llm_json_repair": 1},
  "chunking": {"chunk_size": 800, "chunk_overlap": 120},
  "index_verify": {"ok": true, "chunks": 331, "provider": "bge", "dim": 512, "checks": [...]}
}
```

## POST /ask

请求：

```json
{
  "question": "公司本期营业收入是多少？",
  "history": [{"role": "user", "content": "…"}, {"role": "assistant", "content": "…"}]
}
```

### 知识星图（`ui_action=click_star`）

```jsonc
{
  "ui_action": "click_star",
  "ui_filters": {"node_id": "revenue", "company": "600519"},  // company 必填，见下
  "thread_id": "u-123"
}
```

`ui_filters.company` **必须下发**：不锁定公司时三路召回没有硬过滤，会跨公司混进
不同主体的片段，卡片只能给出"XX 是指……"的通用定义，且页码在多份文档间是歧义 ID
（规则见 `docs/design/nodes.md` 2.3）。

- 未指定 `company` 且库内有多家公司 → 回 `refused=true` 的「请先选择公司」卡片，
  **不会**拿其中一家的资料冒充答案；
- 未指定 `report_period` → 取该公司最新一期，且只作**软偏好**参与检索，不硬过滤；
- 同一 `thread_id` 下公司会被记住：先问"贵州茅台的营业收入"再点节点，主体仍是茅台。

响应：

```jsonc
{
  "answer": "根据《中国国际贸易中心股份有限公司》：\n- 报告期内…18.2 亿元 [P10]\n\n来源：P10、P12\n\n本回答基于公开财报文本自动提取，不构成投资建议。",
  "citations": [{"page": 10, "doc_id": "…", "section_path": "…", "snippet": "…"}],
  "intent": "METRIC",
  "intent_confidence": 1.0,
  "fallback_reason": "",            // 空=正常；非空见下方取值
  "degraded": [],                   // ["rerank"] / ["vector_recall"] / ["llm:fallback_to_mock"] / ["router:llm_fallback_rule"] / ["gate:llm_skipped"] / ["generator:json_fallback_text"] / ["rerank:fallback_heuristic"]
  "fusion_stats": {"count": 20, "top1_score": 0.0414, "top1_relevance": 0.6349, "relevant": true,
                   "table_hits": 10, "pages": [6, 9, 10],
                   "route_hits": {"bm25": 20, "vector": 14}, "multi_route_hits": 14},
  "errors": [],
  "trace_id": "9f2c…",
  "trace": [{"node": "query_rewrite", "latency_ms": 1.2, "ok": true, "error_code": "", "degraded": "", "ts": 1760000000.0}],
  // ↓ 以下为契约层新增字段
  "gate_pass": true,                // 数值闸门是否通过（false 时通常伴随 fallback_reason=num_mismatch）
  "retry_count": 0,                 // retry_shrink 触发次数（>0 说明生成被闸门打回过）
  "route_cfg": {"w_bm25": 1.3, "w_vector": 1.0, "w_meta": 0.8, "top_k": 8,
                "prefer_table": true, "source": "rule", "rule_intent": "METRIC"},
  "generation_used": [],            // 参与生成的 chunk_id 列表；空数组 = 未走结构化生成（Mock 抽取式）
  // ↓ 星图事件（ui_action=click_star）专有
  "unlocked_nodes": ["revenue"],    // 已点亮节点，按 thread_id 跨轮累加
  "ui_payload": {
    "node_id": "revenue", "label": "营业收入",
    "explanation": "贵州茅台2025年度营业收入为人民币16,883,810.25万元 [P54]。",
    "citations": ["P54"],
    "unlock_next": ["net_profit", "gross_margin"],
    "refused": false,
    "unlocked_nodes": ["revenue"],
    "scope": {"stock_code": "600519", "company": "贵州茅台", "report_period": "2025A"}
  }
}
```

`ui_payload.scope` 是引用的**文档坐标**：页码只在「公司 + 期间」下才唯一
（同一家公司有 6 期报告，`P54` 每一期都存在）。它由实际喂给模型的 context 反推，
前端应渲染成「贵州茅台 2025年年度 P54」，而不是裸 `P54`。

`fallback_reason` 取值：`empty` / `low_score` / `num_mismatch` / `generate_failed` / `internal` / `oos` / `clarify` / `chitchat` / `need_company`（星图点击未锁定公司）。

`trace[]` 为**逐节点**记录（按执行顺序），可用它定位慢节点与失败节点：
`ok=false` 时看 `error_code`；`degraded` 非空说明该节点用了降级档位（链路未断）。
三路召回会各产生一条 `bm25_recall` / `vector_recall` / `meta_recall` 记录。

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"公司本期营业收入是多少？"}'
```

## POST /ingest

**默认关闭**。需设置 `VC_INGEST_TOKEN`，并在请求头带 `X-Token`。

```bash
VC_INGEST_TOKEN=dev-token python3 -m uvicorn apps.api:app --port 8000

curl -X POST http://127.0.0.1:8000/ingest \
  -H 'Content-Type: application/json' -H 'X-Token: dev-token' \
  -d '{"path": "./data/sample_report.pdf", "force": false}'
```

```json
{"diff_plan": {"mode": "skip", "reason": "unchanged"}, "upsert_stats": {"skipped": true}, "skipped": true, "errors": [], "trace": [...]}
```

未开启时返回 `404 {"detail":"ingest 接口未开启（需配置 VC_INGEST_TOKEN）"}`；Token 错误返回 `401`。

## 错误约定

- 业务错误**不返回非 2xx**：检索失败、模型失败都由图内兜底，响应体里通过 `fallback_reason` / `errors` 表达；
- 只有鉴权（401）、接口未开启（404）、入参校验（422）会返回非 2xx；
- 所有响应体不含密钥与 chunk 正文全文。
