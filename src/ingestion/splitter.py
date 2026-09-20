"""智能文本分割。

对加载后的 Document 做 RecursiveCharacterTextSplitter 切片，
尽量保持段落与语义完整性。

冷启动优化：langchain_text_splitters 的包 __init__ 会连带导入
sentence_transformers / transformers / torch（实测导入耗时 ~17s，
占服务启动的 94%）。切片仅在文档入库时使用，故改为函数内延迟导入——
问答启动链路完全不碰这些重依赖，首次入库时才付一次导入成本。
"""
from __future__ import annotations

from typing import Any

from config.settings import settings


def get_splitter() -> Any:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
        length_function=len,
    )


def split_documents(docs: list) -> list:
    """将文档切分为语义块。"""
    if not docs:
        return []
    splitter = get_splitter()
    chunks = splitter.split_documents(docs)
    return chunks
