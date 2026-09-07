"""向量库重建脚本。

切换 Embedding 模型后，需重建向量库以保证维度和语义空间一致。

用法：
    python scripts/rebuild_vector_db.py                  # 重建 default 库
    python scripts/rebuild_vector_db.py --kb default     # 指定知识库
    python scripts/rebuild_vector_db.py --all             # 重建所有库
    python scripts/rebuild_vector_db.py --backup          # 备份后重建
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 支持直接 python scripts/rebuild_vector_db.py 运行（不依赖 cwd 在项目根）
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def backup_vector_db():
    """备份当前向量库。"""
    src = PROJECT_ROOT / "vector_db"
    if not src.exists():
        logger.info("向量库目录不存在，跳过备份")
        return None
    import time
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = PROJECT_ROOT / f"vector_db_backup_{ts}"
    shutil.copytree(src, dst)
    logger.info("已备份向量库 → %s", dst)
    return dst


def clear_collection(kb_id: str = "default"):
    """删除指定知识库的 collection（使下次入库时自动重建）。"""
    from src.ingestion.embedder import get_vectorstore

    vs = get_vectorstore(kb_id=kb_id)
    try:
        collection = vs._collection
        ids = collection.get().get("ids", [])
        if not ids:
            # 空库（目录刚删除/首次重建）：无需清空，直接返回
            logger.info("知识库 [%s] 已是空库，无需清空", kb_id)
            return
        collection.delete(ids=ids)
        logger.info("已清空知识库 [%s] 的向量数据（%d 条）", kb_id, len(ids))
    except Exception as e:  # noqa: BLE001
        # 注意：本进程已打开 chroma 的 sqlite 连接，直接 rmtree 会自我锁定
        # （WinError 32）。改为提示用户在干净进程中删除目录后重跑。
        logger.error(
            "清空 collection 失败（%s）。请在项目根目录用独立 Python 进程执行："
            "python -c \"import shutil; shutil.rmtree('vector_db')\" 后重跑本脚本。",
            e,
        )
        raise SystemExit(1)


def reingest_docs(kb_id: str = "default"):
    """批量重建：全部文件 解析→清洗→切片→汇总，按批（100 条）写入。

    v2.5.1：不再逐文件调用 ingest_documents——205 个文件逐条 add 会高频触发
    chroma 后台段压缩，实测在 Windows 上可复现 hnsw 段损坏（Error loading
    hnsw index）。汇总后按批写入将 chroma 写事务从 200+ 次降到 ~15 次。
    """
    from src.ingestion.cleaner import clean_documents
    from src.ingestion.embedder import (
        _chunk_hash,
        get_vectorstore,
        invalidate_corpus,
    )
    from src.ingestion.loader import load_document
    from src.ingestion.splitter import split_documents
    from src.utils.helpers import file_hash, new_doc_id

    uploads_dir = PROJECT_ROOT / "data" / "uploads"
    docs_dir = PROJECT_ROOT / "data" / "docs"

    files = []
    for d in (uploads_dir, docs_dir):
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in (".pdf", ".md", ".txt", ".docx", ".markdown"):
                    files.append(f)

    if not files:
        logger.warning("未找到任何文档文件")
        return {"total": 0, "success": 0, "failed": 0}

    logger.info("找到 %d 个文档，批量重建向量库（100 条/批）...", len(files))
    vs = get_vectorstore(kb_id=kb_id)
    import datetime as _dt

    now_iso = _dt.datetime.now().isoformat(timespec="seconds")
    success, failed = 0, 0
    batch_docs, batch_ids = [], []
    written = 0

    def _flush():
        nonlocal batch_docs, batch_ids, written
        if batch_docs:
            vs.add_documents(batch_docs, ids=batch_ids)
            written += len(batch_docs)
            logger.info("  已写入 %d 块（累计 %d）", len(batch_docs), written)
            batch_docs, batch_ids = [], []

    for i, f in enumerate(files, 1):
        try:
            content_hash = file_hash(f.read_bytes())
            docs = clean_documents(load_document(f, use_ocr=False))
            if not docs:
                logger.warning("[%d/%d] %s → 无文本内容，跳过", i, len(files), f.name)
                failed += 1
                continue
            chunks = split_documents(docs)
            if not chunks:
                logger.warning("[%d/%d] %s → 切片为空，跳过", i, len(files), f.name)
                failed += 1
                continue
            doc_id = new_doc_id()
            for j, c in enumerate(chunks):
                c.metadata["doc_id"] = doc_id
                c.metadata["chunk_index"] = j
                c.metadata.setdefault("source", str(f))
                c.metadata["chunk_hash"] = _chunk_hash(c.page_content)
                c.metadata["content_hash"] = content_hash
                c.metadata["created_at"] = now_iso
                c.metadata["updated_at"] = now_iso
            batch_docs.extend(chunks)
            batch_ids.extend(f"{doc_id}-{j}" for j in range(len(chunks)))
            success += 1
            if len(batch_docs) >= 100:
                _flush()
        except Exception as e:  # noqa: BLE001
            logger.error("[%d/%d] %s → 失败: %s", i, len(files), f.name, e)
            failed += 1
    _flush()
    try:
        invalidate_corpus(kb_id)
    except Exception:  # noqa: BLE001
        pass
    logger.info("重建完成: %d 个文档, 成功 %d, 失败 %d, 共 %d 块", len(files), success, failed, written)
    return {"total": len(files), "success": success, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description="向量库重建工具")
    parser.add_argument("--kb", default="default", help="知识库 ID（默认 default）")
    parser.add_argument("--all", action="store_true", help="重建所有知识库")
    parser.add_argument("--backup", action="store_true", help="重建前备份")
    args = parser.parse_args()

    # 切换到项目目录
    import os
    os.chdir(str(PROJECT_ROOT))

    if args.backup:
        backup_vector_db()

    if args.all:
        # 重建 default 库 + 所有自定义库
        from src.kb.manager import get_kb_manager
        kbs = get_kb_manager().list()
        for kb in kbs:
            kb_id = kb["id"]
            logger.info("===== 重建知识库: %s =====", kb_id)
            clear_collection(kb_id)
            reingest_docs(kb_id)
    else:
        logger.info("===== 重建知识库: %s =====", args.kb)
        clear_collection(args.kb)
        stats = reingest_docs(args.kb)
        logger.info("重建完成: %d 个文档, 成功 %d, 失败 %d", stats["total"], stats["success"], stats["failed"])


if __name__ == "__main__":
    main()
