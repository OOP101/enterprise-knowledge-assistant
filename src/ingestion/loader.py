"""多格式文档加载器。

支持 PDF / Markdown / Text / Word(docx)。
- PDF 优先使用 PyMuPDF 提取文本（含可复制文本）；纯图片页可用 PaddleOCR（可选）。
- 未安装可选依赖时给出友好提示。
"""
from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# 支持的扩展名 -> 处理函数
_SUPPORTED = {".pdf", ".md", ".markdown", ".txt", ".docx"}


def _load_pdf(path: Path, use_ocr: bool = False) -> list[Document]:
    try:
        import pymupdf  # PyMuPDF (fitz)
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore  # 旧版本包名
        except ImportError:
            logger.warning("未安装 PyMuPDF，无法解析 PDF：%s", path)
            return []

    docs: list[Document] = []
    pdf = pymupdf.open(str(path))
    for i, page in enumerate(pdf):
        text = page.get_text().strip()
        if not text and use_ocr:
            text = _ocr_page(page)
        if text:
            docs.append(
                Document(
                    page_content=text,
                    metadata={"source": str(path), "page": i + 1},
                )
            )
    pdf.close()
    return docs


def _ocr_page(page) -> str:
    """使用 PaddleOCR 识别图片页（可选依赖）。"""
    try:
        import numpy as np  # type: ignore
        from paddleocr import PaddleOCR  # type: ignore

        ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        pix = page.get_pixmap()
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        result = ocr.ocr(img, cls=True)
        lines = []
        if result and result[0]:
            for res in result[0]:
                lines.append(res[1][0])
        return "\n".join(lines)
    except ImportError:
        logger.warning("未安装 PaddleOCR，图片页 OCR 已跳过")
        return ""


def _load_markdown(path: Path) -> list[Document]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [Document(page_content=text, metadata={"source": str(path)})]


def _load_text(path: Path) -> list[Document]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [Document(page_content=text, metadata={"source": str(path)})]


def _table_to_markdown(table) -> str:
    """将 docx 表格转换为 Markdown 格式（项目文档规划：表格转 Markdown 入库）。"""
    rows = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if len(rows) >= 1:
        # 在第一行后插入分隔行
        separator = "| " + " | ".join(["---"] * len(table.rows[0].cells)) + " |"
        rows.insert(1, separator)
    return "\n".join(rows)


def _extract_drawing_text(path: Path) -> list[str]:
    """从 docx 的绘图文本框（w:txbxContent）与 SmartArt（a:t）中提取文本。

    背景：流程图类文档的节点文字全部放在 drawing 文本框里，
    python-docx 的 doc.paragraphs 只遍历正文顶层段落，读不到这些内容，
    导致"文档清洗后为空"（实测 011/152/189 三份流程图文件）。
    按文档 XML 出现顺序提取，近似保留流程走向；Choice/Fallback 重复内容去重。
    """
    import re
    import zipfile
    from xml.etree import ElementTree as ET

    namespaces = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    }
    try:
        with zipfile.ZipFile(path) as z:
            xml_bytes = z.read("word/document.xml")
    except Exception:  # noqa: BLE001
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    seen: set[str] = set()
    lines: list[str] = []
    # 遍历所有文本框段落与 DrawingML 文本，保持文档顺序
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag in ("txbxContent",):
            for p in node.iter(f"{{{namespaces['w']}}}p"):
                text = "".join(
                    t.text or "" for t in p.iter(f"{{{namespaces['w']}}}t")
                ).strip()
                if text and text not in seen:
                    seen.add(text)
                    lines.append(text)
        elif tag == "t" and node.tag.startswith(f"{{{namespaces['a']}}}"):
            text = (node.text or "").strip()
            if text and text not in seen:
                seen.add(text)
                lines.append(text)
    # 压缩连续空白/符号节点（流程图连接词等短碎片合并为一行）
    merged = [ln for ln in lines if len(ln) >= 2 or re.search(r"[\u4e00-\u9fff]", ln)]
    return merged


def _load_docx(path: Path) -> list[Document]:
    try:
        from docx import Document as DocxDocument  # type: ignore
    except ImportError:
        logger.warning("未安装 python-docx，无法解析 docx：%s", path)
        return []

    doc = DocxDocument(str(path))

    # 提取段落文本
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    text_parts = list(paragraphs)

    # 提取表格内容并转为 Markdown（避免表格信息丢失）
    table_count = 0
    for table in doc.tables:
        md_table = _table_to_markdown(table)
        if md_table:
            text_parts.append(f"\n\n[表格]\n{md_table}")
            table_count += 1

    # 兜底：正文极稀疏时（纯流程图/SmartArt 文档），提取绘图文本框内容
    body_chars = sum(len(t) for t in text_parts)
    if body_chars < 50:
        drawing_lines = _extract_drawing_text(path)
        if drawing_lines:
            text_parts.append(
                "\n\n[流程图/图形文本]\n" + "\n".join(drawing_lines)
            )
            logger.info(
                "DOCX 绘图文本提取：%s，正文 %d 字符过稀，补提 %d 条文本框/SmartArt 文本",
                path.name, body_chars, len(drawing_lines),
            )

    text = "\n".join(text_parts)
    if table_count:
        logger.info("DOCX 表格提取：%s，共 %d 个表格", path.name, table_count)

    return [Document(page_content=text, metadata={"source": str(path)})]


_LOADERS = {
    ".pdf": _load_pdf,
    ".md": _load_markdown,
    ".markdown": _load_markdown,
    ".txt": _load_text,
    ".docx": _load_docx,
}


def load_document(path: str | Path, use_ocr: bool = False) -> list[Document]:
    """加载单个文档为 Document 列表（PDF 分页）。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in _SUPPORTED:
        raise ValueError(f"不支持的文档格式: {ext}，仅支持 {sorted(_SUPPORTED)}")
    loader = _LOADERS.get(ext)
    if loader is None:
        return []
    docs = loader(path, use_ocr=use_ocr) if ext == ".pdf" else loader(path)
    logger.info("加载 %s：共 %d 段", path.name, len(docs))
    return docs


def load_directory(directory: str | Path, recursive: bool = True) -> list[Document]:
    """加载目录下所有支持格式的文档。"""
    directory = Path(directory)
    if not directory.is_dir():
        logger.warning("目录不存在：%s", directory)
        return []
    pattern = "**/*" if recursive else "*"
    docs: list[Document] = []
    for path in sorted(directory.glob(pattern)):
        if not path.is_file() or path.suffix.lower() not in _SUPPORTED:
            continue
        try:
            docs.extend(load_document(path))
        except Exception as e:  # noqa: BLE001
            logger.exception("加载文档失败：%s，原因：%s", path, e)
    return docs
