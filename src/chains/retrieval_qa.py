"""RetrievalQA 链（LCEL 构建）。

- 真实 LLM：使用 ChatPromptTemplate + format_docs 拼接上下文后调用。
- 演示 LLM：直接传入完整 prompt 字符串，由 DemoLLM 内部解析上下文。
通过 try/except 兼容两类调用，保证离线可用。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from langchain_core.runnables import Runnable

from config.settings import settings
from src.models.llm import get_llm
from src.prompts.templates import QA_SYSTEM, qa_prompt
from src.retrieval.retriever import as_retriever, search_with_context

logger = logging.getLogger(__name__)


def _format_docs(docs) -> str:
    return "\n\n".join(
        f"[{i + 1}] {getattr(d, 'page_content', str(d))}"
        for i, d in enumerate(docs)
    )


def build_retrieval_qa(llm: Any | None = None, top_k: int = 5) -> Runnable:
    """构建 RetrievalQA 链。"""
    llm = llm or get_llm()
    retriever = as_retriever(top_k=top_k)

    # LCEL 标准链路（真实 LLM 场景）
    def _invoke(inputs: dict) -> dict:
        question = inputs.get("question", "")
        docs = retriever.invoke(question)
        context = _format_docs(docs)
        try:
            prompt = qa_prompt.invoke(
                {"context": context, "question": question, "chat_history": []}
            )
            answer = llm.invoke(prompt)
            answer_text = _extract_answer_text(answer)
        except (TypeError, AttributeError):
            # DemoLLM 不兼容消息结构，退化为字符串 prompt
            full_prompt = (
                QA_SYSTEM.format(context=context)
                + "\n\n用户问题："
                + question
            )
            answer_text = llm.invoke(full_prompt)
        return {"answer": answer_text, "sources": docs}

    return RunnableLambdaAdapter(_invoke)


def _extract_answer_text(answer: Any) -> str:
    if isinstance(answer, str):
        return answer
    content = getattr(answer, "content", answer)
    if isinstance(content, list):
        content = " ".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    return str(content) if content is not None else ""


class RunnableLambdaAdapter:
    """极简 Runnable 适配器：统一 `.invoke()` 接口。"""

    def __init__(self, fn) -> None:
        self._fn = fn

    def invoke(self, inputs: dict, config: dict | None = None) -> dict:
        return self._fn(inputs)

    def __or__(self, other):
        return ComposedRunnable(self, other)


class ComposedRunnable:
    def __init__(self, left, right) -> None:
        self._left, self._right = left, right

    def invoke(self, inputs, config=None):
        return self._right.invoke(self._left.invoke(inputs, config))


@lru_cache
def get_retrieval_qa(top_k: int = 5) -> Runnable:
    return build_retrieval_qa(top_k=top_k)


def ask(question: str, top_k: int = 5, kb_id: str = "default") -> dict:
    """便捷入口：单次问答，返回 {answer, sources}。

    集成 v1.5 优化：查询改写 → 检索 → 上下文压缩 → 生成 → 缓存。
    每次动态构建 LLM（读取运行时模型配置），支持模型切换生效。
    """
    from src.models.llm import get_llm
    from src.retrieval.cache import get_query_cache
    from src.retrieval.query_rewriter import rewrite_query
    from src.chains.context_compressor import compress_context
    from src.core.token_tracker import get_token_tracker, estimate_tokens

    # 1. 热查询缓存检查
    cache = get_query_cache()
    cached = cache.get(question, kb_id=kb_id)
    if cached is not None:
        logger.debug("热查询缓存命中：%s", question[:30])
        return {"answer": cached["answer"], "sources": cached["sources"], "cached": True}

    # 2. 查询改写（提升召回率）
    llm = get_llm()
    rewritten = rewrite_query(question, llm=llm)

    # 3. 检索（使用改写后的查询）
    retriever = as_retriever(top_k=top_k, kb_id=kb_id)
    docs = retriever.invoke(rewritten)

    # 4. 上下文压缩（降低 Token 消耗）
    docs = compress_context(docs, query=question)

    context = _format_docs(docs)
    base_sources = [
        {
            "source": getattr(d, "metadata", {}).get("source", ""),
            "content": getattr(d, "page_content", ""),
        }
        for d in docs
    ]

    # 5. 生成回答
    llm = get_llm(context_docs=base_sources)
    full_prompt = QA_SYSTEM.format(context=context) + "\n\n用户问题：" + question
    answer_text = _extract_answer_text(llm.invoke(full_prompt))

    # 6. Token 用量记录
    tracker = get_token_tracker()
    input_tokens = estimate_tokens(full_prompt)
    output_tokens = estimate_tokens(answer_text)
    model_name = getattr(llm, "model_name", getattr(llm, "model", "demo"))
    tracker.record(input_tokens, output_tokens, model=model_name, endpoint="/api/qa/ask")

    # 7. 写入热查询缓存
    cache.put(question, answer_text, base_sources, kb_id=kb_id)

    return {"answer": answer_text, "sources": docs, "cached": False}
