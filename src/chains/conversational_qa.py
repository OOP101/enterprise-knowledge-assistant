"""对话式问答（带记忆，2.0）。

结合短期会话记忆 + 长期向量记忆 + 真指代消解：
1. 先 `recall` 长期记忆注入上下文
2. 用 LLM `condense_prompt` 做真指代消解（而非 1.0 的 return question）
3. 检索 + 生成 + 记录短期/长期记忆
"""
from __future__ import annotations

import logging
from typing import Any

from config.settings import settings
from src.chains.retrieval_qa import _format_docs
from src.core import get_container
from src.models.llm import get_llm
from src.prompts.templates import QA_SYSTEM, condense_prompt
from src.retrieval.retriever import search_with_context

logger = logging.getLogger(__name__)


def _condense_question(question: str, history: list, llm: Any) -> str:
    """真指代消解：调用 LLM 将带指代的最后一句改写为独立问题。

    若历史为空或未启用，则返回原问题。
    保留兼容旧调用；推荐使用 prepare_query（合并消解+改写，单次 LLM 调用）。
    """
    if not history or not settings.condense_history:
        return question
    try:
        prompt = condense_prompt.invoke(
            {"chat_history": history, "question": question}
        )
        resp = llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        text = text.strip()
        return text if text else question
    except Exception as e:  # noqa: BLE001
        logger.debug("指代消解失败，回退原问题：%s", e)
        return question


def prepare_query(question: str, history: list, llm: Any) -> str:
    """检索查询准备（v2.4 合并版）：一次 LLM 调用同时完成指代消解 + 查询改写。

    旧链路是 condense + rewrite 两次调用，思考模型下首字前静默等待 10~30s。
    历史为空时直接返回原问题（零 LLM 开销）。
    """
    if not history or not settings.condense_history:
        return question
    try:
        from src.prompts.templates import search_query_prompt

        prompt = search_query_prompt.invoke(
            {"chat_history": history, "question": question}
        )
        resp = llm.invoke(prompt)
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        text = text.strip('"「」').splitlines()[0].strip() if text else ""
        return text or question
    except Exception as e:  # noqa: BLE001
        logger.debug("查询准备失败，回退原问题：%s", e)
        return question


def _recall_long_memory(session_id: str, question: str) -> list[str]:
    """注入长期向量记忆。"""
    try:
        vm = get_container().get_vector_memory()
        return vm.recall(question, top_k=settings.long_memory_top_k)
    except Exception as e:  # noqa: BLE001
        logger.debug("长期记忆注入失败：%s", e)
        return []


def answer_with_history(session_id: str, question: str, top_k: int = 5, kb_id: str = "default") -> dict:
    """多轮问答主函数：消解指代 → 查询改写 → 注入长期记忆 → 检索 → 上下文压缩 → 生成 → 记记忆。

    kb_id: 指定检索的知识库（默认 default）。
    v1.5 集成：查询改写 + 上下文压缩。
    """
    from src.chains.context_compressor import compress_context
    from src.core.token_tracker import get_token_tracker, estimate_tokens

    container = get_container()
    memory = container.get_session_memory()
    history = memory.get_history(session_id)

    llm = get_llm()

    # 1. 检索查询准备（合并指代消解 + 查询改写，单次调用）
    q = prepare_query(question, history, llm)

    # 3. 注入长期记忆
    long_memory = _recall_long_memory(session_id, q)

    # 4. 检索（限定知识库）
    context, docs = search_with_context(q, top_k=top_k, kb_id=kb_id)

    # 5. 上下文压缩（降低 Token 消耗）
    docs = compress_context(docs, query=question)
    context = "\n\n".join(
        f"[{i + 1}] {getattr(d, 'page_content', str(d))}"
        for i, d in enumerate(docs)
    )

    # 6. 拼接历史 + 长期记忆
    history_text = "\n".join(
        f"{m.type}: {m.content}" for m in history[-settings.memory_k * 2 :]
    )
    base_sources = [
        {
            "source": getattr(d, "metadata", {}).get("source", ""),
            "content": getattr(d, "page_content", ""),
        }
        for d in docs
    ]

    system = QA_SYSTEM.format(context=context)
    if long_memory:
        system += "\n\n【长期记忆】\n" + "\n".join(f"- {m}" for m in long_memory)
    if history_text:
        system += "\n\n【历史对话】\n" + history_text

    # 7. 生成回答
    answer_text = _generate(llm, system, question)

    # 8. Token 用量记录
    tracker = get_token_tracker()
    model_name = getattr(llm, "model_name", getattr(llm, "model", "demo"))
    tracker.record(
        estimate_tokens(system + question), estimate_tokens(answer_text),
        model=model_name, endpoint="/api/qa/chat",
    )

    # 9. 记录短期记忆
    memory.add_turn(session_id, question, answer_text)

    return {
        "answer": answer_text,
        "sources": [
            {
                "source": getattr(d, "metadata", {}).get("source", ""),
                "content": getattr(d, "page_content", ""),
            }
            for d in docs
        ],
    }


def _generate(llm: Any, system: str, question: str) -> str:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = [SystemMessage(content=system), HumanMessage(content=question)]
        answer = llm.invoke(prompt)
        return _extract(answer)
    except (TypeError, AttributeError):
        return _extract(llm.invoke(system + "\n\n用户问题：" + question))


def _extract(answer: Any) -> str:
    if isinstance(answer, str):
        return answer
    content = getattr(answer, "content", answer)
    if isinstance(content, list):
        content = " ".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    return str(content) if content is not None else ""


def clear_session(session_id: str) -> None:
    get_container().get_session_memory().clear(session_id)
