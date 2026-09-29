#!/usr/bin/env python3
"""FastAPI 服务：uvicorn apps.api:app --reload

接口：
POST /ask      问答（返回答案 + 引用 + 融合明细 + 逐节点 trace）
POST /ingest   入库（需 VC_INGEST_TOKEN；未配置则关闭该接口）
GET  /health   健康检查
GET  /stats    知识库统计
GET  /config   运行时配置与落盘校验结果（只读，便于排障）
GET  /starmap  知识星图当前状态（首屏用：不检索、不生成，只读会话快照）

安全：密钥只从环境变量读；/ingest 默认关闭，开启时必须带 X-Token。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src.vc.config import CONFIG  # noqa: E402
from src.vc.graph.ingest_graph import ingest  # noqa: E402
from src.vc.graph.query_graph import ask, starmap_state  # noqa: E402
from src.vc.ingestion.verify import verify_index  # noqa: E402
from src.vc.knowledge import all_docs, known_meta, total_chunks  # noqa: E402

app = FastAPI(title="value_compass", version="0.1.0",
              description="金融垂类 RAG 知识库问答助手（LangGraph 编排）")


class AskRequest(BaseModel):
    # 富交互模式下 question 可为空（点星图节点时只有 ui_filters），故放宽长度约束
    question: str = Field("", max_length=500)
    history: Optional[List[Dict[str, str]]] = None
    ui_action: str = "natural_query"             # click_star / natural_query / clarify_followup / reset_starmap
    ui_filters: Optional[Dict[str, Any]] = None  # {"node_id": "gross_margin", "company": "0700"}
    thread_id: str = "default"                   # 会话维度：unlocked_nodes 靠它跨轮持久化


class IngestRequest(BaseModel):
    path: Optional[str] = None
    force: bool = False


@app.get("/starmap")
def starmap(thread_id: str = "default") -> Dict[str, Any]:
    """知识星图全景：分组、已点亮、可点亮、亮度档位、会话边。

    前端首屏直接渲染它。亮度档位由服务端下发而非前端自算：两边各算一遍必然漂移，
    用户会看到"点了没反应"的节点。
    """
    return starmap_state(thread_id)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "docs": len(all_docs()), "chunks": total_chunks()}


@app.get("/stats")
def stats() -> Dict[str, Any]:
    return {
        "docs": [
            {k: d.get(k) for k in ("company", "stock_code", "report_period", "chunk_count", "doc_version", "status")}
            for d in all_docs()
        ],
        "known_meta": known_meta(),
        "vector_store": CONFIG.vector_store_provider,
        "embedding_provider": CONFIG.embedding_provider,
        "llm_provider": CONFIG.llm_provider,
        "rerank_provider": CONFIG.rerank_provider,
        "index_verify": verify_index(),
    }


@app.get("/config")
def config_api() -> Dict[str, Any]:
    """只读配置快照：排障时先看这里，确认跑的是不是"以为的那套模型"。"""
    return {
        "providers": {
            "embedding": CONFIG.embedding_provider,
            "llm": CONFIG.llm_provider,
            "rerank": CONFIG.rerank_provider,
            "vector_store": CONFIG.vector_store_provider,
        },
        "retrieval": {
            "top_k_bm25": CONFIG.top_k_bm25,
            "top_k_vector": CONFIG.top_k_vector,
            "top_k_meta": CONFIG.top_k_meta,
            "top_k_final": CONFIG.top_k_final,
            "min_top1_relevance": CONFIG.min_top1_relevance,
            "rrf_k": CONFIG.rrf_k,
        },
        "contracts": {
            "router_llm_enabled": CONFIG.router_llm_enabled,
            "gate_llm_enabled": CONFIG.gate_llm_enabled,
            "generator_json_enabled": CONFIG.generator_json_enabled,
            "llm_json_mode": CONFIG.llm_json_mode,
            "llm_json_repair": CONFIG.llm_json_repair,
        },
        "chunking": {"chunk_size": CONFIG.chunk_size, "chunk_overlap": CONFIG.chunk_overlap},
        "index_verify": verify_index(),
    }


@app.post("/ask")
def ask_api(req: AskRequest) -> Dict[str, Any]:
    result = ask(
        req.question,
        history=req.history or [],
        ui_action=req.ui_action or "natural_query",
        ui_filters=req.ui_filters or {},
        thread_id=req.thread_id or "default",
    )
    return {
        "answer": result.get("final_answer", ""),
        "citations": result.get("citations", []),
        # 富交互载荷：前端据此渲染节点卡片与星图连线
        "ui_payload": result.get("ui_payload") or {},
        "unlocked_nodes": result.get("unlocked_nodes", []),
        "intent": result.get("intent"),
        "intent_confidence": result.get("intent_confidence"),
        "fallback_reason": result.get("fallback_reason") or "",
        "degraded": sorted(set(result.get("degraded") or [])),
        "fusion_stats": result.get("fusion_stats", {}),
        "errors": result.get("errors", []),
        "trace_id": result.get("trace_id", ""),
        "trace": result.get("trace", []),
        "gate_pass": result.get("gate_pass"),
        "retry_count": result.get("retry_count", 0),
        "route_cfg": result.get("route_cfg", {}),
        "generation_used": result.get("generation_used", []),
    }


@app.post("/ingest")
def ingest_api(req: IngestRequest, x_token: Optional[str] = Header(None)) -> Dict[str, Any]:
    if not CONFIG.ingest_token:
        raise HTTPException(status_code=404, detail="ingest 接口未开启（需配置 VC_INGEST_TOKEN）")
    if x_token != CONFIG.ingest_token:
        raise HTTPException(status_code=401, detail="token 无效")
    result = ingest(req.path, force_rebuild=req.force)
    return {
        "diff_plan": result.get("diff_plan", {}),
        "upsert_stats": result.get("upsert_stats", {}),
        "bm25_stats": result.get("bm25_stats", {}),
        "skipped": bool(result.get("skipped")),
        "errors": result.get("errors", []),
        "trace": result.get("trace", []),
    }


# web 前端静态目录（web/index.html）。挂在 /web 前缀下，不会遮蔽上面的 API 路由；
# 同源部署也让前端 fetch('/ask') 不必面对 CORS。
from pathlib import Path  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

# 开发期禁缓存：StaticFiles 只发 ETag 不发 Cache-Control，改完前端刷新还是旧页面
# （表现为"新功能明明写了却不生效"），必须显式 no-store。
@app.middleware("http")
async def _nocache_web(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/web"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

_WEB_DIR = Path(__file__).resolve().parents[1] / "web"
if _WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CONFIG.api_host, port=CONFIG.api_port)
