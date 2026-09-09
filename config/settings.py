"""全局配置。

通过 pydantic-settings 从环境变量 / .env 文件加载配置。
所有配置项带默认值，保证无 .env 时也能以"演示模式"启动。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用全局配置。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- LLM ----
    llm_api_base: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.2
    # 思考模式：off=对支持的服务商(如 MiMo)传 thinking disabled，显著降低首字延迟
    # on=保持思考（质量优先，慢）；auto=不干预由服务商默认行为决定
    llm_thinking: str = "off"

    # ---- Embedding ----
    # Provider: dashscope(原生) | openai(OpenAI兼容) | local(sentence-transformers) | demo(哈希兜底)
    embedding_provider: str = "dashscope"
    # 使用 DashScope text-embedding-v3（1024 维）。
    # 注意：DeepSeek 无 OpenAI 兼容 embedding 接口，Embedding 必须走 DashScope。
    embedding_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-v3"
    # 本地模型（sentence-transformers），离线可用，无需 API Key
    embedding_local_model: str = "BAAI/bge-small-zh-v1.5"

    # ---- Vector Store ----
    vector_store: str = "chroma"  # chroma | milvus
    # 注意：必须使用相对路径。chromadb(1.3.x) 的 Rust hnsw 段层在 torch 已加载的
    # 进程中，若收到含非 ASCII 字符（如中文目录名）的绝对路径，段二进制文件会
    # 静默不落盘（sqlite 正常），跨进程查询报「Error loading hnsw index」。
    # 相对路径字符串在进程内由 OS 以宽字符 API 解析，不受影响（已实测验证）。
    chroma_persist_dir: str = "vector_db"
    chroma_collection: str = "enterprise_knowledge"
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "enterprise_knowledge"

    # ---- Chunking ----
    chunk_size: int = 512
    chunk_overlap: int = 64

    # ---- Rerank（2.0 可插拔：demo | qwen | bge）----
    reranker_provider: str = "demo"  # demo(默认兜底) / qwen(在线) / bge(本地 FlagEmbedding)
    reranker_model: str = "qwen-rerank-v1"

    # ---- Retriever ----
    retriever_hybrid: bool = True

    # ---- Memory（2.0）----
    memory_k: int = 5
    long_memory_top_k: int = 3
    condense_history: bool = True  # 是否做指代消解

    # ---- Auth / Multi-KB（2.0）----
    auth_enabled: bool = False  # 默认关闭，保证演示可直接访问
    jwt_secret: str = "change-me-in-prod"
    jwt_expire_minutes: int = 720
    default_kb_id: str = "default"
    # 预置管理员：首次启动时若不存在则自动创建（仅当 auth_enabled=true 时生效）
    admin_username: str = "admin"
    admin_password: str = "admin123"

    # ---- Agent 工具（2.0）----
    workflow_api_base: str = ""  # 非空则真实 HTTP 调用流程引擎
    workflow_timeout: float = 10.0

    # ---- Eval（2.0）----
    eval_top_k: int = 5

    # ---- v1.5 优化 ----
    query_rewrite: bool = True  # 是否启用查询改写
    context_compress: bool = True  # 是否启用上下文压缩
    context_token_budget: int = 6000  # 上下文压缩 Token 预算
    cache_enabled: bool = True  # 是否启用热查询缓存
    cache_size: int = 100  # 缓存容量
    cache_ttl: int = 3600  # 缓存过期时间（秒）

    # ---- Review（v3.0 入库审核流）----
    review_enabled: bool = True  # 总开关：关闭后上传直接入库（与 2.x 行为一致）
    review_mode: str = "threshold"  # threshold=低分推送人工审 | all=全量人工审 | off=仅打分不拦截
    review_score_threshold: int = 60  # 低于该分数推送人工审核（0-100）
    raw_docs_dir: str = str(PROJECT_ROOT / "data" / "raw_docs")  # 原文缓存（原始库）

    # ---- Service ----
    host: str = "0.0.0.0"
    port: int = 8008

    # ---- Paths ----
    docs_dir: str = str(PROJECT_ROOT / "data" / "docs")
    uploads_dir: str = str(PROJECT_ROOT / "data" / "uploads")

    @property
    def llm_ready(self) -> bool:
        """判断是否配置了可用的 LLM Key（未配置时启用演示模式）。"""
        return bool(self.llm_api_key)

    def ensure_dirs(self) -> None:
        """确保运行所需的目录存在。"""
        for d in (self.docs_dir, self.uploads_dir, self.chroma_persist_dir, self.raw_docs_dir):
            Path(d).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
