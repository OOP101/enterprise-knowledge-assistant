"""多格式文档加载器。

支持 PDF / Markdown / Text / Word(docx) / Word 97-2003(doc)。
- PDF 优先使用 PyMuPDF 提取文本（含可复制文本）；纯图片页可用 PaddleOCR（可选）。
- .doc 走 olefile + 编码探测粗提取（无 Word / LibreOffice 依赖，不还原表格与排版）。
- 未安装可选依赖时给出友好提示。
"""
from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# 支持的扩展名（批量入库扫描与格式校验共用，避免两处漂移）
SUPPORTED_EXTENSIONS = frozenset(
    {".pdf", ".md", ".markdown", ".txt", ".docx", ".doc", ".xlsx"}
)


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


# Word 97-2003 (.doc) 二进制 FIB 结构偏移
_FIB_FLAGS_OFF = 0x0A  # 位 2 = fComplex，位 9 = fWhichTblStm
_FIB_FCMIN_OFF = 0x18
_FIB_FCMAC_OFF = 0x1C
_FIB_FCCLX_OFF = 0x01A2
_FIB_LCBCLX_OFF = 0x01A6
_PCD_SIZE = 8
_CLXT_GRPPRL = 0x01
_CLXT_PLCFPCD = 0x02


def _doc_text_runs(fib: bytes, table: bytes) -> list[tuple[int, int, bool]]:
    """解析 FIB 的 piece table，返回 [(流偏移, 字符数, 是否 8 位压缩)]。

    Word 97+ 的正文由若干 piece 拼成，每片可独立选用 8 位(cp1252) 或 16 位(UTF-16LE)
    编码——按片解码比"整体猜编码"可靠，也天然避开 FIB 头部等二进制结构。
    """
    flags = int.from_bytes(fib[_FIB_FLAGS_OFF:_FIB_FLAGS_OFF + 2], "little")

    if not flags & 0x0004:  # fComplex=0：正文是单块 8 位文本
        fc_min = int.from_bytes(fib[_FIB_FCMIN_OFF:_FIB_FCMIN_OFF + 4], "little")
        fc_mac = int.from_bytes(fib[_FIB_FCMAC_OFF:_FIB_FCMAC_OFF + 4], "little")
        return [(fc_min, max(0, fc_mac - fc_min), True)] if fc_mac > fc_min else []

    fc_clx = int.from_bytes(fib[_FIB_FCCLX_OFF:_FIB_FCCLX_OFF + 4], "little")
    lcb_clx = int.from_bytes(fib[_FIB_LCBCLX_OFF:_FIB_LCBCLX_OFF + 4], "little")
    clx = table[fc_clx:fc_clx + lcb_clx]

    # CLX = 若干 Prc(0x01) + 一个 Pcdt(0x02)
    i = 0
    while i < len(clx) and clx[i] == _CLXT_GRPPRL:
        if i + 3 > len(clx):
            return []
        i += 3 + int.from_bytes(clx[i + 1:i + 3], "little")
    if i >= len(clx) or clx[i] != _CLXT_PLCFPCD or i + 5 > len(clx):
        return []

    lcb = int.from_bytes(clx[i + 1:i + 5], "little")
    plc = clx[i + 5:i + 5 + lcb]
    n = (len(plc) - 4) // 12  # 前 n+1 个 int32 是字符边界，其后每 8 字节一个 PCD
    if n <= 0:
        return []

    cps = [int.from_bytes(plc[k * 4:k * 4 + 4], "little") for k in range(n + 1)]
    pcd_off = (n + 1) * 4
    runs: list[tuple[int, int, bool]] = []
    for k in range(n):
        pcd = plc[pcd_off + k * _PCD_SIZE:pcd_off + (k + 1) * _PCD_SIZE]
        if len(pcd) < _PCD_SIZE:
            break
        fc = int.from_bytes(pcd[2:6], "little")
        compressed = bool(fc & 0x40000000)
        fc &= 0x3FFFFFFF
        if compressed:
            fc //= 2  # 压缩片：偏移以"半字节"为单位存的是 8 位流位置
        runs.append((fc, cps[k + 1] - cps[k], compressed))
    return runs


def _normalize_doc_text(text: str) -> str:
    """清洗 .doc 正文中的排版控制符，保留段落结构。"""
    # \r 段落标记 / \x0b 手动换行 / \x0c 分页 → 统一换行；\xa0 不间断空格 → 普通空格
    text = text.replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
    text = text.replace("\xa0", " ").replace("\x00", "")
    lines = (ln.strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def _load_doc(path: Path) -> list[Document]:
    """解析 Word 97-2003 二进制 .doc。

    走 olefile + piece table，零外部依赖——无需本机安装 Word / WPS / LibreOffice。
    属纯文本粗提取：保留段落结构，但不还原表格与排版，复杂版式可能失真。
    """
    try:
        import olefile
    except ImportError:
        logger.warning("未安装 olefile，无法解析 doc：%s", path)
        return []

    try:
        with olefile.OleFileIO(str(path)) as ole:
            if not ole.exists("WordDocument"):
                logger.warning("非 Word 二进制格式（缺 WordDocument 流）：%s", path)
                return []
            fib = ole.openstream("WordDocument").read()
            flags = int.from_bytes(fib[_FIB_FLAGS_OFF:_FIB_FLAGS_OFF + 2], "little")
            tbl_name = "1Table" if (flags >> 9) & 1 else "0Table"
            table = ole.openstream(tbl_name).read() if ole.exists(tbl_name) else b""

            parts: list[str] = []
            for fc, n_chars, compressed in _doc_text_runs(fib, table):
                if n_chars <= 0 or fc < 0:
                    continue
                if compressed:
                    parts.append(fib[fc:fc + n_chars].decode("cp1252", errors="ignore"))
                else:
                    parts.append(fib[fc:fc + n_chars * 2].decode("utf-16-le", errors="ignore"))
    except Exception:  # noqa: BLE001
        logger.exception("doc 解析失败：%s", path)
        return []

    text = _normalize_doc_text("".join(parts))
    if not text.strip():
        logger.warning("doc 提取后无有效文本：%s", path)
        return []
    logger.info("DOC 文本提取：%s，共 %d 字符", path.name, len(text))
    return [Document(page_content=text, metadata={"source": str(path)})]


# OOXML 命名空间
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XLSX_PKG_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _xlsx_sheet_titles(zf) -> dict[str, str]:
    """工作表文件路径 -> 工作表标题（best-effort；结构异常时返回空表，不影响正文提取）。"""
    from xml.etree import ElementTree as ET

    try:
        book = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except Exception:  # noqa: BLE001
        return {}

    target_by_id = {
        r.get("Id"): r.get("Target", "") for r in rels.findall(f"{_XLSX_PKG_NS}Relationship")
    }
    titles: dict[str, str] = {}
    for sheet in book.iter(f"{_XLSX_NS}sheet"):
        target = target_by_id.get(sheet.get(f"{_XLSX_REL_NS}id"), "").lstrip("/")
        if not target:
            continue
        if not target.startswith("xl/"):
            target = "xl/" + target
        titles[target] = sheet.get("name") or target
    return titles


def _xlsx_cell_text(cell, shared: list[str]) -> str:
    """单元格文本：内联串(t=inlineStr) / 共享串(t=s) / 字面值。"""
    if cell.get("t") == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{_XLSX_NS}t"))
    v = cell.find(f"{_XLSX_NS}v")
    if v is None or v.text is None:
        return ""
    if cell.get("t") == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    return v.text


def _load_xlsx(path: Path) -> list[Document]:
    """解析 Excel 2007+（.xlsx）。

    xlsx 本质是 zip + XML，用标准库读取即可，零外部依赖（无需 openpyxl）。
    逐工作表输出「| 单元格 | 单元格 |」行文本，并保留表名以便检索定位。
    """
    import re
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        with zipfile.ZipFile(str(path)) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                ss = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                shared = [
                    "".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t"))
                    for si in ss.iter(f"{_XLSX_NS}si")
                ]

            sheets = sorted(
                n for n in zf.namelist()
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
            )
            titles = _xlsx_sheet_titles(zf)

            parts: list[str] = []
            for sheet in sheets:
                rows: list[str] = []
                root = ET.fromstring(zf.read(sheet))
                for row in root.iter(f"{_XLSX_NS}row"):
                    cells = [
                        _xlsx_cell_text(c, shared).strip()
                        for c in row.findall(f"{_XLSX_NS}c")
                    ]
                    while cells and not cells[-1]:  # 去掉行尾空单元格（保留列间空位）
                        cells.pop()
                    if any(cells):
                        rows.append("| " + " | ".join(cells) + " |")
                if rows:
                    parts.append(f"\n[{titles.get(sheet, sheet)}]\n" + "\n".join(rows))
    except Exception:  # noqa: BLE001
        logger.exception("xlsx 解析失败：%s", path)
        return []

    text = "\n".join(parts).strip()
    if not text:
        logger.warning("xlsx 提取后无有效文本：%s", path)
        return []
    logger.info("XLSX 表格提取：%s，共 %d 个工作表", path.name, len(parts))
    return [Document(page_content=text, metadata={"source": str(path)})]


_LOADERS = {
    ".pdf": _load_pdf,
    ".md": _load_markdown,
    ".markdown": _load_markdown,
    ".txt": _load_text,
    ".docx": _load_docx,
    ".doc": _load_doc,
    ".xlsx": _load_xlsx,
}


def load_document(path: str | Path, use_ocr: bool = False) -> list[Document]:
    """加载单个文档为 Document 列表（PDF 分页）。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持的文档格式: {ext}，仅支持 {sorted(SUPPORTED_EXTENSIONS)}")
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
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            docs.extend(load_document(path))
        except Exception as e:  # noqa: BLE001
            logger.exception("加载文档失败：%s，原因：%s", path, e)
    return docs
