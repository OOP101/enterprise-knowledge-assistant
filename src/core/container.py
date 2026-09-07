"""依赖注入组合根（Composition Root）。

2.0 架构核心：所有具体实现通过 Container 统一装配并缓存（单例），
消除 1.0 中模块级 `@lru_cache` 单例 + 紧耦合 import 带来的循环依赖与难测试问题。

用法：
    from src.core import get_container
    container = get_container()
    retriever = container.get_retriever("default")
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from config.settings import Settings, get_settings
from src.core.interfaces import BaseReranker, SessionMemoryPort, VectorMemoryPort


class Container:
    """应用依赖容器。所有能力按需装配、缓存，可覆盖注入（用于测试）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._singletons: dict[str, Any] = {}

    # ---- 覆盖注入（测试用）----
    def override(self, key: str, impl: Any) -> None:
        self._singletons[key] = impl

    def _get_or_create(self, key: str, factory: Any) -> Any:
        if key not in self._singletons:
            self._singletons[key] = factory()
        return self._singletons[key]

    # ---- Embedding ----
    def get_embedding(self) -> Any:
        from src.models.embedding import get_embedding

        return self._get_or_create("embedding", get_embedding)

    # ---- Vector Store（按知识库）----
    def get_vectorstore(self, kb_id: str = "default") -> Any:
        from src.ingestion.embedder import get_vectorstore

        key = f"vectorstore:{kb_id}"
        return self._get_or_create(
            key,
            lambda: get_vectorstore(collection_name=self._collection_name(kb_id)),
        )

    def _collection_name(self, kb_id: str) -> str:
        base = self.settings.chroma_collection
        if kb_id in ("", "default"):
            return base
        return f"{base}_{kb_id}"

    # ---- Reranker（可插拔）----
    def get_reranker(self) -> BaseReranker:
        from src.models.reranker import build_reranker

        return self._get_or_create("reranker", build_reranker)

    # ---- Retriever ----
    def get_retriever(
        self,
        kb_id: str = "default",
        top_k: int = 5,
        use_hybrid: bool | None = None,
    ) -> Any:
        from src.retrieval.retriever import HybridRetriever

        use_hybrid = self.settings.retriever_hybrid if use_hybrid is None else use_hybrid
        key = f"retriever:{kb_id}:{top_k}:{use_hybrid}"
        return self._get_or_create(
            key,
            lambda: HybridRetriever(
                top_k=top_k,
                use_hybrid=use_hybrid,
                reranker=self.get_reranker(),
                kb_id=kb_id,  # 构造时固定知识库，实例间互不串扰
            ),
        )

    # ---- Memory ----
    def get_session_memory(self) -> SessionMemoryPort:
        from src.memory.conversation_memory import ConversationMemory

        return self._get_or_create(
            "session_memory",
            lambda: ConversationMemory(max_turns=self.settings.memory_k),
        )

    def get_vector_memory(self) -> VectorMemoryPort:
        from src.memory.vector_memory import VectorMemory

        return self._get_or_create(
            "vector_memory",
            lambda: VectorMemory(
                embedding=self.get_embedding(),
                persist_dir=self.settings.chroma_persist_dir,
            ),
        )

    # ---- LLM ----
    def get_llm(self) -> Any:
        from src.models.llm import get_llm

        return self._get_or_create("llm", get_llm)


@lru_cache
def get_container() -> Container:
    """返回全局容器（进程级单例）。"""
    return Container(get_settings())
