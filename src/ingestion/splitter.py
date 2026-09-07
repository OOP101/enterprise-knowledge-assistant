"""智能文本分割。

对加载后的 Document 做 RecursiveCharacterTextSplitter 切片，
尽量保持段落与语义完整性。
"""
from __future__ import annotations

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config.settings import settings


def get_splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
        length_function=len,
    )


def split_documents(docs: list[Document]) -> list[Document]:
    """将文档切分为语义块。"""
    if not docs:
        return []
    splitter = get_splitter()
    chunks = splitter.split_documents(docs)
    return chunks
