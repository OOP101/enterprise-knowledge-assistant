"""向量检索器封装（含混合检索与重排序，2.0）。

对外提供统一的检索入口，Reranker 可插拔（通过构造函数注入）：
- ``retrieve()``：兼容 ``BaseRetrieverPort``（Eval / 工具场景按需指定 top_k）
- ``search_with_context()``：混合检索 + 重排序，返回 (context, docs)
- ``as_retriever()``：标准 BaseRetriever（供 LangChain 链使用）

注意：``kb_id`` 在实例构造时固定，检索器实例之间互不影响，避免共享缓存实例
被并发修改（1.0 曾直接在缓存的单例上改 ``kb_id``，存在竞态）。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from src.core.interfaces import BaseReranker
from src.retrieval.hybrid_search import dense_search, hybrid_search


class HybridRetriever(BaseRetriever):
    """可配置的混合检索器（LCEL 兼容，Reranker 可注入，支持知识库隔离）。"""

    use_hybrid: bool = True
    top_k: int = 5
    reranker: BaseReranker | None = None
    kb_id: str = "default"

    def retrieve(self, query: str, top_k: int = 5) -> list[Document]:
        """检索并返回精排后的 top_k 文档（兼容 BaseRetrieverPort）。

        top_k 仅作用于本次调用，不修改实例配置，可安全并发使用。
        """
        if self.use_hybrid:
            docs = hybrid_search(query, top_k=top_k * 3, kb_id=self.kb_id)
        else:
            docs = dense_search(query, top_k=top_k * 3, kb_id=self.kb_id)
        if self.reranker is not None:
            return self.reranker.rerank(query, docs, top_k=top_k)
        return docs[:top_k]

    def _get_relevant_documents(self, query: str) -> list[Document]:
        return self.retrieve(query, top_k=self.top_k)

    def search_with_context(self, query: str, top_k: int = 5) -> tuple[str, list[Document]]:
        """检索并拼接上下文。返回 (context_text, docs)。"""
        docs = self.retrieve(query, top_k=top_k)
        context = "\n\n".join(
            f"[{i + 1}] {getattr(d, 'page_content', str(d))}"
            for i, d in enumerate(docs)
        )
        return context, docs


@lru_cache
def get_retriever(
    top_k: int = 5, use_hybrid: bool = True, kb_id: str = "default"
) -> HybridRetriever:
    from src.core import get_container

    return get_container().get_retriever(kb_id=kb_id, top_k=top_k, use_hybrid=use_hybrid)


def as_retriever(
    top_k: int = 5, use_hybrid: bool = True, kb_id: str = "default"
) -> Any:
    """返回标准 BaseRetriever（供 RetrievalQA / ConversationalRetrievalChain 使用）。"""
    return get_retriever(top_k=top_k, use_hybrid=use_hybrid, kb_id=kb_id)


def search_with_context(
    query: str,
    top_k: int = 5,
    use_hybrid: bool = True,
    kb_id: str = "default",
) -> tuple[str, list[Document]]:
    """检索并拼接上下文。返回 (context_text, docs)。

    每次构建与 kb_id 绑定的独立检索器实例（经 lru_cache 缓存），
    不再修改共享实例，保证多知识库并发检索互不串扰。
    """
    retriever = get_retriever(top_k=top_k, use_hybrid=use_hybrid, kb_id=kb_id)
    docs = retriever.invoke(query)
    context = "\n\n".join(
        f"[{i + 1}] {getattr(d, 'page_content', str(d))}"
        for i, d in enumerate(docs)
    )
    return context, docs
