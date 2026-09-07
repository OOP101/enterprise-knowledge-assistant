"""通用工具函数。"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any


def safe_filename(name: str) -> str:
    """清洗文件名，避免路径穿越。"""
    return Path(name).name


def file_hash(content: bytes) -> str:
    """计算文件内容 MD5（用于文档去重与增量更新）。"""
    return hashlib.md5(content).hexdigest()


def new_doc_id() -> str:
    """生成文档 ID。"""
    return uuid.uuid4().hex


def format_sources(docs: list[Any]) -> list[dict]:
    """将检索结果格式化为 API 友好的 JSON。"""
    sources = []
    for d in docs:
        meta = dict(getattr(d, "metadata", {}) or {})
        sources.append(
            {
                "source": meta.get("source", "unknown"),
                "doc_id": meta.get("doc_id", ""),
                "page": meta.get("page"),
                "score": getattr(d, "score", None),
                "content": getattr(d, "page_content", str(d)),
            }
        )
    return sources
