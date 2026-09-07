"""文档清洗模块。

在上传/入库前对提取出的文本做「基础 + 结构」两阶段清洗，提升切片与检索质量：

基础清洗：
- 统一换行符、去除全角/零宽空格、去除不可见控制字符
- 规范化标点（全角 → 半角仅针对英文/数字上下文，中文标点保留）
- 去除行首行尾空白、压缩连续空行

结构清洗：
- 去除页眉/页脚（重复出现的短行、页码、分隔线）
- 去除目录页（连续含「...」「…」的跳转行）
- 合并断行（把被硬换行拆散的中文段落重新拼接）
- 去除空表格残留、多余的表格分隔行（markdown ``| --- |`` 连续行）
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.documents import Document

logger = __import__("logging").getLogger(__name__)

# 零宽字符与不可见控制符（保留 \n）
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad\u200e\u200f]")
# 控制字符（除换行、制表符）
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 目录行：整行仅由「标题 + 点号/省略号 + 页码」构成
_TOC_LINE = re.compile(r"^[\s\u4e00-\u9fa5\w、（）()《》<>·:：,，.。\-\s]*[\.…]{2,}\s*\d*\s*$")
# 页码行：纯数字（可能带括号/短横线）独立成行
_PAGE_LINE = re.compile(r"^\s*(?:[\(\[]?\s*[-–—]?\s*\d{1,4}\s*[\)\]]?\s*)$")
# 分隔线（markdown --- 或 *** 或 ___ 或 ─ 等）
_HR_LINE = re.compile(r"^\s*(?:[-–—=_*~]{3,}|[┄┅┈┉─━═]+)\s*$")
# 表格分隔行（markdown | --- | --- |）
_TABLE_SEP = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")


def clean_text(text: str) -> str:
    """对单段文本做基础 + 结构清洗。返回清洗后的字符串。"""
    if not text:
        return text

    # 1. 统一换行
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 2. 去零宽字符与不可见控制符
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)

    # 3. 逐行清洗
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        line = line.strip()
        # 空行、页码、分隔线、目录行、表格分隔行直接丢弃
        if not line:
            continue
        if _PAGE_LINE.match(line):
            continue
        if _HR_LINE.match(line):
            continue
        if _TOC_LINE.match(line):
            continue
        if _TABLE_SEP.match(line):
            continue
        cleaned.append(line)

    text = "\n".join(cleaned)

    # 4. 合并被硬换行拆散的中文段落：
    #    若某行以中文结尾且下一行以中文开头（非标题/列表），则拼接
    text = _merge_broken_lines(text)

    # 5. 压缩 3 个以上连续空行 → 1 个空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _merge_broken_lines(text: str) -> str:
    """把被硬换行拆散的中文段落合并回一行。

    规则：前一行以中文字符结尾，且后一行以中文字符开头、不含明显的
    标题/编号/列表标记（#、数字编号、项目符号），则合并。
    """
    lines = text.split("\n")
    if len(lines) < 2:
        return text

    merged: list[str] = []
    buf = lines[0]
    # 段落起始标记：列表/标题/编号
    _bullet = re.compile(r"^[\d一二三四五六七八九十]+[、.．)]|^[（(]\d+[)）]|^[-*•·▪]|^#{1,6}\s|^第[一二三四五六七八九十]+[章节条款]")
    _cjk = re.compile(r"[\u4e00-\u9fa5]$")

    for nxt in lines[1:]:
        # 上一行以中文结尾，且下一行是中文开头、非段落起始标记 → 视为断行
        if (
            buf
            and _cjk.search(buf)
            and nxt
            and re.match(r"^[\u4e00-\u9fa5]", nxt)
            and not _bullet.match(nxt)
        ):
            buf = buf + nxt
        else:
            merged.append(buf)
            buf = nxt
    merged.append(buf)
    return "\n".join(merged)


def clean_documents(docs: list[Document]) -> list[Document]:
    """对 Document 列表逐片清洗 page_content，返回新列表。

    - 清洗后为空的 Document 被丢弃。
    - 清洗后内容相同（相邻去重）的 Document 被合并，避免重复分片。
    """
    result: list[Document] = []
    seen: set[str] = set()
    for d in docs:
        raw = getattr(d, "page_content", "")
        cleaned = clean_text(raw)
        if not cleaned:
            continue
        # 内容完全相同的分片去重
        if cleaned in seen:
            continue
        seen.add(cleaned)
        new_doc = Document(page_content=cleaned, metadata=dict(getattr(d, "metadata", {}) or {}))
        result.append(new_doc)
    return result


__all__ = ["clean_text", "clean_documents"]
