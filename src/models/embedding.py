"""Embedding 模型实例化（多 Provider 支持）。

支持四种 Provider（通过 EMBEDDING_PROVIDER 配置）：
1. ``dashscope``：DashScope 原生接口（text-embedding-v3, 1024 维）
2. ``openai``：OpenAI 兼容接口（如 SiliconFlow / Zhipu / OpenAI 本身）
3. ``local``：本地 sentence-transformers 模型（BGE 系列，离线可用）
4. ``demo``：基于 jieba 分词的哈希向量（无外部依赖兜底）

降级链路：dashscope/openai Key 失效 → demo 哈希向量（维度自动对齐已有集合）。
"""
from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


class DemoEmbeddings:
    """演示 Embedding：基于 jieba 分词的词频哈希向量。

    维度默认 512（与当前 vector_db 中 collection 的维度对齐，避免维度不匹配）。
    哈希使用 md5（跨进程稳定），保证重启后仍能检索历史入库数据。

    注意：ChromaDB collection 维度在创建时锁定。若 vector_db 里已是 1024 维
    （text-embedding-v3 写入），改回 512 会触发 `InvalidArgumentError: expecting 512,
    got 1024`。改此维度必须与 collection 实际维度保持一致，否则需清空 vector_db 重建。
    """

    dimension: int = 512

    def __init__(self) -> None:
        self._jieba = None
        try:
            import jieba  # type: ignore

            self._jieba = jieba
        except ImportError:
            self._jieba = None

    def _tokens(self, text: str) -> list[str]:
        if self._jieba is not None:
            return list(self._jieba.cut(text))
        text = text.lower()
        return [text[i : i + 2] for i in range(max(0, len(text) - 1))]

    @staticmethod
    def _stable_index(token: str, dim: int) -> int:
        """md5 稳定哈希：跨进程/重启结果一致。"""
        return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % dim

    def _vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for tok in self._tokens(text):
            idx = self._stable_index(tok, self.dimension)
            vec[idx] += 1.0
        # L2 归一化
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorize(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vectorize(text)


class LocalEmbeddings:
    """本地 sentence-transformers 模型（BGE 系列）。

    首次使用时自动下载模型（缓存至 ~/.cache/huggingface）。
    离线可用，无需 API Key，适合企业内网部署。
    """

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(model_name)
            self._dim = self._model.get_sentence_embedding_dimension()
            logger.info("本地 Embedding 模型加载完成：%s（维度=%d）", model_name, self._dim)
        except ImportError:
            raise ImportError(
                "未安装 sentence-transformers，请运行：pip install sentence-transformers"
            )

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._model.encode([text], normalize_embeddings=True)[0].tolist()


class _FallbackEmbeddings:
    """真实 Embedding 的降级包装（熔断器）。

    首次调用失败（Key 无效 / 网络异常等）后，自动切换到 DemoEmbeddings 并告警，
    避免每次请求都等待网络超时，也保证「演示完备、离线可跑」不因坏 Key 而 500。
    """

    def __init__(self, primary: Any, fallback: Any) -> None:
        self._primary = primary
        self._fallback = fallback
        self._use_fallback = False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._use_fallback:
            return self._fallback.embed_documents(texts)
        try:
            return self._primary.embed_documents(texts)
        except Exception as e:  # noqa: BLE001
            self._use_fallback = True
            logger.warning("真实 Embedding 调用失败（%s），已降级为本地演示向量", e)
            return self._fallback.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        if self._use_fallback:
            return self._fallback.embed_query(text)
        try:
            return self._primary.embed_query(text)
        except Exception as e:  # noqa: BLE001
            self._use_fallback = True
            logger.warning("真实 Embedding 调用失败（%s），已降级为本地演示向量", e)
            return self._fallback.embed_query(text)


def _try_dashscope() -> Any:
    """DashScope 原生 Embedding。"""
    from langchain_community.embeddings import DashScopeEmbeddings

    return DashScopeEmbeddings(
        model=settings.embedding_model,
        dashscope_api_key=settings.embedding_api_key,
    )


def _try_openai_compatible() -> Any:
    """OpenAI 兼容 Embedding 接口（SiliconFlow / Zhipu / OpenAI 等）。"""
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_api_base,
    )


def _try_local() -> Any:
    """本地 sentence-transformers 模型。"""
    return LocalEmbeddings(model_name=settings.embedding_local_model)


@lru_cache
def get_embedding() -> Any:
    """返回全局 Embedding 实例。

    Provider 优先级（由 EMBEDDING_PROVIDER 配置决定）：
    - dashscope：DashScope 原生（需 Key + dashscope SDK）
    - openai：OpenAI 兼容接口（需 Key）
    - local：本地 BGE 模型（需 sentence-transformers，无需 Key）
    - demo：哈希向量兜底（无外部依赖）

    dashscope / openai 在 Key 缺失或调用失败时自动降级到 demo，
    保证链路始终可用。
    """
    provider = (settings.embedding_provider or "dashscope").lower()

    # local 模式：直接使用本地模型，无降级
    if provider == "local":
        try:
            return _try_local()
        except ImportError as e:
            logger.warning("本地 Embedding 不可用（%s），降级到 demo", e)
            return DemoEmbeddings()
        except Exception as e:  # noqa: BLE001
            logger.warning("本地 Embedding 加载失败（%s），降级到 demo", e)
            return DemoEmbeddings()

    # demo 模式：直接返回哈希向量
    if provider == "demo":
        return DemoEmbeddings()

    # dashscope / openai：需要 API Key，无 Key 时降级 demo
    if not settings.embedding_api_key:
        logger.info("Embedding Provider=%s 但未配置 API Key，降级到 demo 哈希向量", provider)
        return DemoEmbeddings()

    primary: Any = None
    try:
        if provider == "dashscope":
            primary = _try_dashscope()
        elif provider == "openai":
            primary = _try_openai_compatible()
    except ImportError as e:
        logger.warning("Provider=%s 依赖缺失（%s），降级到 demo", provider, e)
        return DemoEmbeddings()
    except Exception as e:  # noqa: BLE001
        logger.warning("Provider=%s 初始化失败（%s），降级到 demo", provider, e)
        return DemoEmbeddings()

    # 熔断器：调用期失败自动降级 demo
    return _FallbackEmbeddings(primary, DemoEmbeddings())


def get_embedding_dimension() -> int:
    """返回当前 Embedding 模型的向量维度（用于启动时维度校验）。"""
    emb = get_embedding()
    dim = getattr(emb, "dimension", None)
    if dim is not None:
        return dim
    # 对于 LangChain Embeddings，尝试 embed 一条空文本获取维度
    try:
        vec = emb.embed_query("维度探测")
        return len(vec)
    except Exception:  # noqa: BLE001
        return 1024  # 默认假设
