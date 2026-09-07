"""上下文压缩模块。

当检索结果过长时，对每个分片提取摘要，控制送入 LLM 的上下文在 Token 预算内。
项目文档规划：「上下文压缩策略 → Token 消耗降低 60%」

两种模式：
1. LLM 摘要压缩：调用 LLM 对每个分片生成摘要（质量高）
2. 规则截断压缩：按句子重要度截断（离线兜底）
"""
from __future__ import annotations

import logging
import re
from typing import Any

from langchain_core.documents import Document

from config.settings import settings
from src.models.llm import get_llm

logger = logging.getLogger(__name__)

# 默认 Token 预算（约 4000 Token ≈ 6000 中文字符）
DEFAULT_TOKEN_BUDGET = 6000
# 单个分片压缩后最大字符数
MAX_CHUNK_CHARS = 500


def _estimate_tokens(text: str) -> int:
    """粗略估算 Token 数：中文约 1 字 ≈ 1.5 Token，英文约 4 字符 ≈ 1 Token。"""
    chinese_chars = len(re.findall(r"[\u4e00-\u9fa5]", text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars * 1.5 + other_chars / 4)


def _rule_compress_chunk(doc: Document, max_chars: int = MAX_CHUNK_CHARS) -> Document:
    """规则压缩：按句子重要度截取关键信息。"""
    text = doc.page_content
    if len(text) <= max_chars:
        return doc

    # 按句子切分
    sentences = re.split(r"[。\n；！？]", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return doc

    # 取前 max_chars 字符的句子
    compressed = []
    current_len = 0
    for s in sentences:
        if current_len + len(s) > max_chars:
            break
        compressed.append(s)
        current_len += len(s)

    compressed_text = "。".join(compressed) + "。"
    return Document(page_content=compressed_text, metadata=dict(doc.metadata))


def _llm_compress_chunk(doc: Document, query: str, llm: Any) -> Document:
    """LLM 摘要压缩：提取与问题最相关的信息。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "请将以下文档片段压缩为不超过 200 字的摘要，保留与问题相关的关键信息。\n"
        "只输出摘要内容，不要输出其他内容。"
    )
    prompt = [
        SystemMessage(content=system),
        HumanMessage(content=f"问题：{query}\n\n文档片段：{doc.page_content}"),
    ]
    resp = llm.invoke(prompt)
    summary = resp.content if hasattr(resp, "content") else str(resp)
    summary = summary.strip()
    if not summary:
        return doc
    return Document(page_content=summary, metadata=dict(doc.metadata))


def compress_context(
    docs: list[Document],
    query: str,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    llm: Any | None = None,
) -> list[Document]:
    """压缩上下文：当检索结果超过 Token 预算时触发压缩。

    Args:
        docs: 检索到的文档列表
        query: 用户问题（用于 LLM 摘要时聚焦相关信息）
        token_budget: 最大 Token 预算（超出则触发压缩）
        llm: 可选的 LLM 实例（未提供时从全局获取）

    Returns:
        压缩后的文档列表（Token 总量在预算内）
    """
    if not docs:
        return docs

    if not settings.context_compress:
        return docs

    full_text = "\n\n".join(d.page_content for d in docs)
    estimated_tokens = _estimate_tokens(full_text)

    token_budget = settings.context_token_budget or token_budget

    if estimated_tokens <= token_budget:
        return docs

    logger.info(
        "上下文压缩触发：原始 %d Token → 目标 ≤ %d Token（%d 个分片）",
        estimated_tokens, token_budget, len(docs),
    )

    if llm is None:
        llm = get_llm()

    compressed: list[Document] = []
    if settings.llm_ready:
        try:
            for doc in docs:
                compressed.append(_llm_compress_chunk(doc, query, llm))
        except Exception as e:
            logger.debug("LLM 摘要压缩失败，回退规则截断：%s", e)
            compressed = [_rule_compress_chunk(doc) for doc in docs]
    else:
        compressed = [_rule_compress_chunk(doc) for doc in docs]

    # 二次检查：如果压缩后仍超预算，按最大字符数截断
    total_chars = sum(len(d.page_content) for d in compressed)
    if total_chars > token_budget:
        ratio = token_budget / total_chars
        for doc in compressed:
            max_len = int(MAX_CHUNK_CHARS * ratio)
            if len(doc.page_content) > max_len:
                doc.page_content = doc.page_content[:max_len] + "..."

    final_tokens = _estimate_tokens(
        "\n\n".join(d.page_content for d in compressed)
    )
    reduction = (1 - final_tokens / estimated_tokens) * 100
    logger.info(
        "上下文压缩完成：%d → %d Token（降低 %.0f%%）",
        estimated_tokens, final_tokens, reduction,
    )

    return compressed
