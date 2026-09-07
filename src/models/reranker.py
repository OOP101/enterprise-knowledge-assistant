"""重排序模型实例化（2.0 可插拔）。

支持两种真实重排方案 + 一个演示兜底：
1. `qwen`：DashScope/OpenAI 兼容 `qwen-rerank` 在线接口
2. `bge`：本地 `FlagEmbedding`（BGE-Reranker，需 pip install FlagEmbedding）
3. `demo`：1.0 字符重合兜底（无任何外部依赖可跑）

通过 `config/settings.py` 的 `reranker_provider` 选择，不影响调用方。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from langchain_core.documents import Document

from config.settings import settings
from src.core.interfaces import BaseReranker

logger = logging.getLogger(__name__)


class DemoReranker(BaseReranker):
    """演示重排器：基于查询词与文档词字符重合度打分（1.0 保留作兜底）。"""

    def rerank(self, query: str, docs: list[Document], top_k: int = 5) -> list[Document]:
        q_words = set(query.lower())
        scored = []
        for d in docs:
            text = getattr(d, "page_content", str(d))
            score = sum(1 for w in text.lower() if w in q_words and not w.isspace())
            d.metadata["score"] = float(score)
            scored.append(d)
        scored.sort(key=lambda d: d.metadata.get("score", 0.0), reverse=True)
        return scored[:top_k]

    def name(self) -> str:
        return "demo"


class QwenReranker(BaseReranker):
    """在线 Qwen-Rerank（DashScope 原生接口）。"""

    def __init__(self, api_key: str, model: str = "qwen-rerank-v1") -> None:
        self._api_key = api_key
        self._model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            import dashscope  # type: ignore

            from dashscope import TextReRank

            self._client = TextReRank
        return self._client

    def rerank(self, query: str, docs: list[Document], top_k: int = 5) -> list[Document]:
        client = self._get_client()
        documents = [getattr(d, "page_content", str(d)) for d in docs]
        try:
            resp = client.call(
                model=self._model,
                query=query,
                documents=documents,
                top_n=top_k,
                api_key=self._api_key,
            )
            ranked: list[Document] = []
            for item in resp.output.results:
                idx = item.index
                score = float(item.relevance_score)
                d = docs[idx]
                d.metadata["score"] = score
                ranked.append(d)
            return ranked
        except Exception as e:  # noqa: BLE001
            logger.warning("QwenReranker 调用失败，回退字符重合：%s", e)
            return DemoReranker().rerank(query, docs, top_k=top_k)

    def name(self) -> str:
        return f"qwen:{self._model}"


class BGEReranker(BaseReranker):
    """本地 BGE-Reranker（FlagEmbedding）。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-base") -> None:
        from FlagEmbedding import FlagReranker  # type: ignore

        self._model = FlagReranker(model_name, use_fp16=False)

    def rerank(self, query: str, docs: list[Document], top_k: int = 5) -> list[Document]:
        pairs = [(query, getattr(d, "page_content", str(d))) for d in docs]
        try:
            scores = self._model.compute_score(pairs, normalize=True)
            if isinstance(scores, float):
                scores = [scores]
            scored = list(zip(docs, scores))
            scored.sort(key=lambda x: x[1], reverse=True)
            result = []
            for d, s in scored[:top_k]:
                d.metadata["score"] = float(s)
                result.append(d)
            return result
        except Exception as e:  # noqa: BLE001
            logger.warning("BGEReranker 调用失败，回退字符重合：%s", e)
            return DemoReranker().rerank(query, docs, top_k=top_k)

    def name(self) -> str:
        return f"bge:{self._model}"


def build_reranker() -> BaseReranker:
    """依据配置构建重排器（provider 可插拔，失败自动回退 demo）。"""
    provider = (settings.reranker_provider or "demo").lower()
    if provider == "qwen":
        if settings.embedding_api_key:
            return QwenReranker(api_key=settings.embedding_api_key, model=settings.reranker_model)
        logger.warning("reranker_provider=qwen 但未配置 Key，回退 demo")
    elif provider == "bge":
        try:
            return BGEReranker(model_name=settings.reranker_model)
        except ImportError as e:
            logger.warning("未安装 FlagEmbedding，回退 demo：%s", e)
    return DemoReranker()


@lru_cache
def get_reranker() -> BaseReranker:
    return build_reranker()
