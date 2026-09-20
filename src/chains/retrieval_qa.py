"""单次问答链路（v1.5 优化版）。

流程：热查询缓存 → 查询改写 → 混合检索 → 上下文压缩 → LLM 生成 → Token 记录 → 写缓存。

历史说明：1.0 的 LCEL 链构建（build_retrieval_qa / RunnableLambdaAdapter）已被
该便捷入口取代且无调用方，已于 3.1.1 清理。
"""
from __future__ import annotations

import logging

from src.models.llm import get_llm
from src.prompts.templates import QA_SYSTEM
from src.retrieval.retriever import as_retriever

logger = logging.getLogger(__name__)


def _format_docs(docs) -> str:
    return "\n\n".join(
        f"[{i + 1}] {getattr(d, 'page_content', str(d))}"
        for i, d in enumerate(docs)
    )


def _extract_answer_text(answer) -> str:
    if isinstance(answer, str):
        return answer
    content = getattr(answer, "content", answer)
    if isinstance(content, list):
        content = " ".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    return str(content) if content is not None else ""


def ask(question: str, top_k: int = 5, kb_id: str = "default") -> dict:
    """便捷入口：单次问答，返回 {answer, sources}。

    集成 v1.5 优化：查询改写 → 检索 → 上下文压缩 → 生成 → 缓存。
    每次动态构建 LLM（读取运行时模型配置），支持模型切换生效。
    """
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

    # 2. 查询改写：单轮问题较完整，规则改写（口语化归一 + 同义词扩展）即可，
    #    省一次 LLM 网络往返（v2.5 起流式链路无历史时同样零 LLM 开销，两链路对齐）
    rewritten = rewrite_query(question, use_llm=False)

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
