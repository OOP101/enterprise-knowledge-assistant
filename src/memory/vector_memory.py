"""长期向量记忆（2.0：接通 QA 链路，可注入 embedding/persist_dir）。

将对话历史的关键结论向量化存储，支持跨会话检索（用户可长期"记住"的信息）。
支持通过构造参数注入，便于测试与容器装配。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from langchain_core.documents import Document

from config.settings import settings
from src.core.interfaces import VectorMemoryPort
from src.models.embedding import get_embedding
from src.utils.helpers import new_doc_id

logger = logging.getLogger(__name__)


def _build_store(embedding: Any, persist_dir: str) -> Any:
    try:
        from langchain_chroma import Chroma

        return Chroma(
            embedding_function=embedding,
            collection_name="conversation_memory",
            persist_directory=persist_dir,
        )
    except ImportError:
        from langchain_community.vectorstores import InMemoryVectorStore

        return InMemoryVectorStore(embedding=embedding)


class VectorMemory(VectorMemoryPort):
    """长期向量记忆（线程安全，可注入依赖）。"""

    def __init__(
        self,
        embedding: Any | None = None,
        persist_dir: str | None = None,
        collection_name: str = "conversation_memory",
    ) -> None:
        self._embedding = embedding or get_embedding()
        self._store = _build_store(
            self._embedding, persist_dir or settings.chroma_persist_dir
        )
        self._collection_name = collection_name

    def remember(self, session_id: str, user: str, summary: str) -> str:
        doc = Document(
            page_content=summary,
            metadata={"session_id": session_id, "user": user},
        )
        mid = new_doc_id()
        try:
            self._store.add_documents([doc], ids=[mid])
        except Exception as e:  # noqa: BLE001
            logger.warning("长期记忆写入失败：%s", e)
        return mid

    def recall(self, query: str, top_k: int = 3) -> list[str]:
        try:
            docs = self._store.similarity_search(query, k=top_k)
            return [getattr(d, "page_content", "") for d in docs]
        except Exception as e:  # noqa: BLE001
            logger.debug("长期记忆检索失败：%s", e)
            return []


@lru_cache
def get_vector_memory() -> VectorMemory:
    return VectorMemory()


def remember(session_id: str, user: str, summary: str) -> str:
    """持久化一条记忆（兼容旧接口）。"""
    return get_vector_memory().remember(session_id, user, summary)


def recall(query: str, top_k: int = 3) -> list[str]:
    """跨会话检索相关记忆（兼容旧接口）。"""
    return get_vector_memory().recall(query, top_k=top_k)
