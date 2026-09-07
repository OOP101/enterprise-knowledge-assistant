"""企业知识助手 FastAPI 入口。

启动：
    uvicorn serve.main:app --reload --port 8008
或直接运行： python -m serve.main

访问 http://localhost:8008 即可打开可视化主界面。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from serve.routers import auth, chat, eval, kb, knowledge, model, qa, review, upload

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保目录存在
    settings.ensure_dirs()
    # 2.0：初始化结构化日志 + 默认知识库
    from src.core.tracing import setup_logging

    setup_logging()
    from src.kb.manager import get_kb_manager

    get_kb_manager().ensure_default()

    # 向量段自愈：清理 chroma 半初始化段目录（必须在任何向量库读写之前）
    if settings.vector_store.lower() == "chroma":
        try:
            from src.ingestion.embedder import repair_chroma_segments

            repair_chroma_segments()
        except Exception:  # noqa: BLE001
            pass

    # 维度校验：检查 Embedding 模型与已有向量库维度是否匹配
    _check_embedding_dimension()
    # 安全自检：默认密钥/默认管理员密码检测
    _check_security_settings()
    # 认证开启时，预置管理员账号（不存在才创建）
    ensure_admin_user()
    yield


def _check_security_settings() -> None:
    """启动期安全自检：防止带着默认密钥/默认密码上线。

    - 认证开启 + JWT_SECRET 为默认值 → 拒绝启动（可被伪造任意 token）
    - JWT_SECRET 含 "change-me" / ADMIN_PASSWORD 为默认值 → 强警告
    """
    import logging

    logger = logging.getLogger(__name__)
    if not settings.auth_enabled:
        return
    if settings.jwt_secret == "change-me-in-prod":
        raise RuntimeError(
            "检测到 JWT_SECRET 使用默认值 change-me-in-prod，认证开启时禁止启动。"
            "请在 .env 中设置强随机密钥"
            "（可用 python -c \"import secrets; print(secrets.token_hex(32))\" 生成）"
        )
    if "change-me" in settings.jwt_secret:
        logger.warning("⚠ JWT_SECRET 含有 'change-me' 字样，疑似未完全替换，建议更换为强随机密钥")
    if settings.admin_password == "admin123":
        logger.warning("=" * 68)
        logger.warning("⚠ 管理员密码仍为默认值 admin123，存在账号被接管风险！")
        logger.warning("  请修改 .env 中 ADMIN_PASSWORD；若 admin 账号已存在，")
        logger.warning("  可登录后调用 PATCH /api/auth/{username}/update 或删除 data/users.json 重启")
        logger.warning("=" * 68)


def ensure_admin_user() -> None:
    """认证开启时，确保存在预置管理员账号（admin）。"""
    if not settings.auth_enabled:
        return
    from src.auth.users import get_user_store

    store = get_user_store()
    if store.get(settings.admin_username) is None:
        try:
            store.register(
                settings.admin_username,
                settings.admin_password,
                role="admin",
                department="",
                extra_kbs=[],
            )
        except ValueError as e:
            # 密码不满足强度策略等情况：不阻断启动，提示手动处理
            import logging

            logging.getLogger(__name__).error(
                "预置管理员账号创建失败：%s（请检查 ADMIN_PASSWORD 是否过短）", e
            )


def _check_embedding_dimension() -> None:
    """启动时检查 Embedding 维度与已有向量库是否匹配。

    维度不匹配时输出警告，提示用户运行重建脚本。
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from src.models.embedding import get_embedding_dimension
        from src.ingestion.embedder import get_vectorstore

        emb_dim = get_embedding_dimension()
        vs = get_vectorstore()
        collection = vs._collection
        existing_count = collection.count()
        if existing_count > 0:
            # 取一条数据检查维度
            sample = collection.get(include=["embeddings"], limit=1)
            embeddings = sample.get("embeddings", [])
            if embeddings:
                vec_dim = len(embeddings[0])
                if vec_dim != emb_dim:
                    logger.warning(
                        "⚠ Embedding 维度不匹配：当前模型=%d 维，已有向量库=%d 维。"
                        "新文档入库可能失败。请运行：python scripts/rebuild_vector_db.py --backup",
                        emb_dim, vec_dim,
                    )
    except Exception as e:  # noqa: BLE001
        logger.debug("维度校验跳过（%s）", e)


def create_app() -> FastAPI:
    app = FastAPI(
        title="企业知识助手",
        description="基于 RAG 的企业知识问答系统：语义检索 + 多轮对话记忆 + 工具调用闭环",
        version="2.3.0",
        lifespan=lifespan,
    )

    # CORS（允许前端跨域调用）
    # 注意：allow_origins=["*"] 与 allow_credentials=True 互斥（浏览器会拒绝携带凭据的跨域请求）。
    # 认证基于 Authorization 头而非 Cookie，故此处不携带凭据，使用通配来源即可。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 禁用静态资源缓存（开发期保证前端改动即时生效）
    @app.middleware("http")
    async def no_cache_static(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            # 强制文本类静态资源声明 UTF-8，避免 Windows 下 Chrome 出现"错误编码"警告
            ct = response.headers.get("Content-Type", "")
            if ct.startswith("text/") or ct == "application/javascript":
                if "charset=" not in ct.lower():
                    response.headers["Content-Type"] = f"{ct}; charset=utf-8"
        return response

    # 注册 API 路由
    app.include_router(qa.router)
    app.include_router(upload.router)
    app.include_router(knowledge.router)
    app.include_router(model.router)
    app.include_router(auth.router)
    app.include_router(kb.router)
    app.include_router(chat.router)
    app.include_router(eval.router)
    app.include_router(review.router)

    @app.get("/api/health", tags=["系统"])
    def health() -> dict:
        return {
            "service": "企业知识助手",
            "version": "2.3.1",
            "docs": "/docs",
            "mode": "production" if settings.llm_ready else "demo",
        }

    # 挂载前端静态资源（开发期禁用缓存，确保前端改动即时生效）
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", tags=["前端"], include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            str(STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # 浏览器由 start.bat / start.sh 负责打开（服务就绪后显式打开，更可靠）
    # 统一监听 IPv4(0.0.0.0)。浏览器/脚本均用 127.0.0.1 访问，避免
    # localhost 解析为 IPv6(::1) 及 Windows 双栈 "::" 导致 IPv4 连接被拒的问题。
    # reload 由环境变量控制：
    #   - 默认关闭 reload。项目根目录会持续写入日志/数据文件，
    #     开启 reload 会导致 WatchFiles 无限重载、终端闪烁、且易产生幽灵 socket。
    #   - 开发确需热重载时，显式设置 RELOAD=1 即可（注意仅监听 src/serve 等稳定目录）。
    _reload = os.getenv("RELOAD", "0").strip().lower() not in ("0", "false", "no", "off")
    uvicorn.run(
        "serve.main:app",
        host=settings.host,      # 配置默认 0.0.0.0（纯 IPv4）
        port=settings.port,
        reload=_reload,
    )
