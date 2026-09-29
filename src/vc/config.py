"""全局配置。

安全约定：
- 任何密钥（API Key / Token）只允许从环境变量读取，禁止写入代码或提交到仓库。
- 需要密钥时复制 .env.example 为 .env 并填写，程序通过 python-dotenv 加载。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """加载 ROOT/.env；已存在的环境变量优先（命令行注入 > .env）。

    python-dotenv 为可选依赖，缺失时静默跳过——此时仍可用真实环境变量完成配置。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


_load_dotenv()


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _has_package(name: str) -> bool:
    """探测可选依赖是否可用（只查不导入，避免拖慢启动）。"""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _b(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # ---------- 路径 ----------
    data_dir: Path = ROOT / "data"
    index_dir: Path = ROOT / "index"
    chroma_dir: Path = ROOT / "index" / "chroma"
    bm25_path: Path = ROOT / "index" / "bm25.pkl"
    manifest_path: Path = ROOT / "index" / "manifest.json"
    default_pdf: Path = ROOT / "data" / "sample_report.pdf"

    # ---------- Provider 选择 ----------
    embedding_provider: str = "auto"        # auto | hashing | bge | openai_compat（auto=有 sentence-transformers 则用 bge）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    hashing_dim: int = 384

    vector_store_provider: str = "chroma"   # chroma | pickle
    llm_provider: str = "mock"              # mock | fake | openai_compat
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_json_mode: bool = True              # OpenAI 兼容 response_format=json_object
    llm_json_repair: int = 1                # Schema 校验失败后的修复重试次数
    rerank_provider: str = "heuristic"      # heuristic | bge | none
    rerank_model: str = "BAAI/bge-reranker-base"

    # ---------- 真实模型（可选依赖）----------
    embedding_batch_size: int = 32
    embedding_device: str = ""              # 空=自动选择 mps/cuda/cpu

    # ---------- LLM 契约层开关（失败均自动回落确定性逻辑）----------
    router_llm_enabled: bool = True         # 规则低置信时是否用 LLM 判定意图
    gate_llm_enabled: bool = True           # 数值闸门是否叠加 LLM 语义判定
    generator_json_enabled: bool = True     # generator 是否走 AnswerResult JSON

    # ---------- 分块 ----------
    chunk_size: int = 800
    chunk_overlap: int = 120
    min_chunk_chars: int = 120

    # ---------- 召回与融合 ----------
    top_k_bm25: int = 20
    top_k_vector: int = 20
    top_k_meta: int = 10
    top_k_final: int = 5
    rrf_k: int = 60
    w_bm25: float = 1.0
    w_vector: float = 1.0
    w_meta: float = 0.6
    table_boost: float = 1.15
    low_score_min: float = 0.008            # top1 RRF 低于该值判为低置信
    # IDF 加权相关性阈值。实测权衡（22 条金标 + 60 条 CFQA 语料外），调高=更"惜答"：
    # 0.28 -> 金标 引用/闸门 86.4%、兜底 9.1% ｜ 语料外诚实 43.3%
    # 0.34 -> 金标 77.3%、兜底 18.2%      ｜ 语料外诚实 50.0%
    # 0.46 -> 金标 77.3%、兜底 22.7%      ｜ 语料外诚实 63.3%
    min_top1_relevance: float = 0.28

    # ---------- 生成与上下文 ----------
    context_token_budget: int = 4000
    max_retry: int = 2

    # ---------- 超时（秒）----------
    # 预算按"单路空载"标定会偏乐观：BM25 全量打分实测 1.0–1.6s/次，而 bm25_recall 与
    # metadata_recall 是并行两路、共享 GIL，累加后远超单路耗时（3.0s 曾让两路双双超时，
    # 召回塌成一路 -> 转兜底）。5.0s 是"两路累加 + 冷启动抖动"的余量。
    # 语料再大时用 TIMEOUT_BM25 环境变量上调。
    timeout_bm25: float = 5.0
    timeout_vector: float = 5.0
    timeout_rerank: float = 3.0
    timeout_llm: float = 30.0

    # ---------- 熔断 ----------
    circuit_threshold: int = 3
    circuit_ttl: float = 60.0

    # ---------- 路由阈值 ----------
    intent_confidence_min: float = 0.6
    intent_margin_min: float = 0.15

    # ---------- 服务 ----------
    ingest_token: str = ""                  # /ingest 接口的简单鉴权，空则关闭该接口
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # ---------- 知识星图（v0.5）----------
    # 热度：直接点击记满分，被间接关联记半分；封顶防单点刷爆。
    heat_direct: float = 1.0
    heat_indirect: float = 0.5
    heat_cap: float = 20.0
    # 衰退（D6）：按 wall clock 半衰，与"用户多久没看"同构，非按轮次计数。
    decay_half_life_sec: float = 7 * 24 * 3600.0
    # 亮度兜底：低于此值强制最低档（正常情况由分档决定）
    visible_eps: float = 0.02
    # 引导脉冲（v0.5 改为"临时提档"持续时间，不是连续衰减）
    pulse_decay_sec: float = 2.0

    # 分档（v0.5）：绝对数量锚定 + 绝对门限前置，替代 v0.4 的按比例切档。
    # 按比例会①跳变（点亮 A 导致无关节点 B 掉档）②在整体衰退后仍造出"最亮"的虚假差异。
    tier_bright_n: int = 3            # last_seen 最近的 N 个
    tier_dim_n: int = 3               # last_seen 最久的 N 个
    tier_min_brightness: float = 0.15 # max(brightness) 低于此值 → 不分档，全部 dim
    tier_min_nodes: int = 6           # 已点亮 ≤ N 个 → 全部 mid（否则 bright 与 dim 重叠）

    # 会话边（v0.5）：上限 20 会让 37 节点图的直径压到 2~3，clickable≈全部，
    # "探索前沿"机制自我瓦解。5 条是"够用但不毁拓扑"的量级。
    max_session_edges: int = 5

    # ---------- 合规 ----------
    disclaimer: str = "本回答基于公开财报文本自动提取，不构成投资建议。"

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        cfg.data_dir = Path(_s("VC_DATA_DIR", str(cfg.data_dir)))
        cfg.index_dir = Path(_s("VC_INDEX_DIR", str(cfg.index_dir)))
        cfg.chroma_dir = Path(_s("VC_CHROMA_DIR", str(cfg.chroma_dir)))
        cfg.bm25_path = Path(_s("VC_BM25_PATH", str(cfg.bm25_path)))
        cfg.manifest_path = Path(_s("VC_MANIFEST_PATH", str(cfg.manifest_path)))
        cfg.default_pdf = Path(_s("VC_DEFAULT_PDF", str(cfg.default_pdf)))

        cfg.embedding_provider = _s("EMBEDDING_PROVIDER", cfg.embedding_provider)
        if cfg.embedding_provider == "auto":
            # 装了 sentence-transformers 就用真向量，否则回落零依赖 hashing
            cfg.embedding_provider = "bge" if _has_package("sentence_transformers") else "hashing"
        cfg.embedding_model = _s("EMBEDDING_MODEL", cfg.embedding_model)
        cfg.hashing_dim = _i("HASHING_DIM", cfg.hashing_dim)

        cfg.vector_store_provider = _s("VECTOR_STORE_PROVIDER", cfg.vector_store_provider)
        cfg.llm_provider = _s("LLM_PROVIDER", cfg.llm_provider)
        cfg.llm_model = _s("LLM_MODEL", cfg.llm_model)
        cfg.llm_base_url = _s("LLM_BASE_URL", cfg.llm_base_url)
        cfg.llm_json_mode = _b("LLM_JSON_MODE", cfg.llm_json_mode)
        cfg.llm_json_repair = _i("LLM_JSON_REPAIR", cfg.llm_json_repair)
        cfg.rerank_provider = _s("RERANK_PROVIDER", cfg.rerank_provider)
        cfg.rerank_model = _s("RERANK_MODEL", cfg.rerank_model)

        cfg.embedding_batch_size = _i("EMBEDDING_BATCH_SIZE", cfg.embedding_batch_size)
        cfg.embedding_device = _s("EMBEDDING_DEVICE", cfg.embedding_device)

        cfg.router_llm_enabled = _b("ROUTER_LLM_ENABLED", cfg.router_llm_enabled)
        cfg.gate_llm_enabled = _b("GATE_LLM_ENABLED", cfg.gate_llm_enabled)
        cfg.generator_json_enabled = _b("GENERATOR_JSON_ENABLED", cfg.generator_json_enabled)

        cfg.chunk_size = _i("CHUNK_SIZE", cfg.chunk_size)
        cfg.chunk_overlap = _i("CHUNK_OVERLAP", cfg.chunk_overlap)
        cfg.min_chunk_chars = _i("MIN_CHUNK_CHARS", cfg.min_chunk_chars)

        cfg.top_k_bm25 = _i("TOP_K_BM25", cfg.top_k_bm25)
        cfg.top_k_vector = _i("TOP_K_VECTOR", cfg.top_k_vector)
        cfg.top_k_meta = _i("TOP_K_META", cfg.top_k_meta)
        cfg.top_k_final = _i("TOP_K_FINAL", cfg.top_k_final)
        cfg.rrf_k = _i("RRF_K", cfg.rrf_k)
        cfg.w_bm25 = _f("W_BM25", cfg.w_bm25)
        cfg.w_vector = _f("W_VECTOR", cfg.w_vector)
        cfg.w_meta = _f("W_META", cfg.w_meta)
        cfg.table_boost = _f("TABLE_BOOST", cfg.table_boost)
        cfg.low_score_min = _f("LOW_SCORE_MIN", cfg.low_score_min)
        cfg.min_top1_relevance = _f("MIN_TOP1_RELEVANCE", cfg.min_top1_relevance)

        cfg.context_token_budget = _i("CONTEXT_TOKEN_BUDGET", cfg.context_token_budget)
        cfg.max_retry = _i("MAX_RETRY", cfg.max_retry)

        cfg.timeout_bm25 = _f("TIMEOUT_BM25", cfg.timeout_bm25)
        cfg.timeout_vector = _f("TIMEOUT_VECTOR", cfg.timeout_vector)
        cfg.timeout_rerank = _f("TIMEOUT_RERANK", cfg.timeout_rerank)
        cfg.timeout_llm = _f("TIMEOUT_LLM", cfg.timeout_llm)

        cfg.circuit_threshold = _i("CIRCUIT_THRESHOLD", cfg.circuit_threshold)
        cfg.circuit_ttl = _f("CIRCUIT_TTL", cfg.circuit_ttl)

        cfg.intent_confidence_min = _f("INTENT_CONFIDENCE_MIN", cfg.intent_confidence_min)
        cfg.intent_margin_min = _f("INTENT_MARGIN_MIN", cfg.intent_margin_min)

        cfg.ingest_token = _s("VC_INGEST_TOKEN", cfg.ingest_token)
        cfg.api_host = _s("VC_API_HOST", cfg.api_host)
        cfg.api_port = _i("VC_API_PORT", cfg.api_port)

        cfg.heat_direct = _f("VC_HEAT_DIRECT", cfg.heat_direct)
        cfg.heat_indirect = _f("VC_HEAT_INDIRECT", cfg.heat_indirect)
        cfg.heat_cap = _f("VC_HEAT_CAP", cfg.heat_cap)
        cfg.decay_half_life_sec = _f("VC_DECAY_HALF_LIFE_SEC", cfg.decay_half_life_sec)
        cfg.visible_eps = _f("VC_VISIBLE_EPS", cfg.visible_eps)
        cfg.pulse_decay_sec = _f("VC_PULSE_DECAY_SEC", cfg.pulse_decay_sec)
        cfg.tier_bright_n = _i("VC_TIER_BRIGHT_N", cfg.tier_bright_n)
        cfg.tier_dim_n = _i("VC_TIER_DIM_N", cfg.tier_dim_n)
        cfg.tier_min_brightness = _f("VC_TIER_MIN_BRIGHTNESS", cfg.tier_min_brightness)
        cfg.tier_min_nodes = _i("VC_TIER_MIN_NODES", cfg.tier_min_nodes)
        cfg.max_session_edges = _i("VC_MAX_SESSION_EDGES", cfg.max_session_edges)

        cfg.index_dir.mkdir(parents=True, exist_ok=True)
        return cfg


CONFIG = Config.from_env()


def get_api_key(env_name: str = "OPENAI_API_KEY") -> Optional[str]:
    """读取密钥；缺失返回 None，由调用方决定降级。"""
    key = os.environ.get(env_name)
    return key or None
