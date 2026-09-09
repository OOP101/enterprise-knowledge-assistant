"""向量库重建脚本（原生 chromadb 版 v3）。

v3 重写缘由：经逐项二分验证，凡经由 langchain_chroma 参与 collection
创建/写入的重建流程，hnsw 段二进制（data_level0.bin 等）均无法跨进程
落盘（sqlite 有数据、段目录只剩 index_metadata.pickle），查询报
「Error loading hnsw index」。而单实例原生 API 流程反复验证可靠：

    PersistentClient → get_or_create_collection(cosine)
    → LocalEmbeddings 计算 BGE 向量 → 分批 col.add（原生）
    → system.stop()（触发最终 flush）→ 段文件完整性自检

用法：
    python scripts/rebuild_vector_db.py                  # 重建 default 库
    python scripts/rebuild_vector_db.py --kb default     # 指定知识库
    python scripts/rebuild_vector_db.py --skip-verify    # 跳过落盘自检
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def reingest_native(kb_id: str = "default") -> dict:
    """单实例原生写入：文档解析 → 切片 → BGE 向量 → col.add。"""
    import chromadb

    from src.ingestion.cleaner import clean_documents
    from src.ingestion.loader import load_document
    from src.ingestion.splitter import split_documents
    from src.models.embedding import get_embedding
    from src.utils.helpers import file_hash, new_doc_id

    from src.ingestion.embedder import _chunk_hash

    collection_name = "enterprise_knowledge" if kb_id == "default" else f"kb_{kb_id}"
    persist_dir = "vector_db"  # 相对路径：规避 torch+中文绝对路径下 Rust 段层静默失败

    client = chromadb.PersistentClient(path=persist_dir)
    col = client.get_or_create_collection(
        name=collection_name, metadata={"hnsw:space": "cosine"}
    )
    logger.info("原生 collection [%s] 就绪（persist=%s）", collection_name, persist_dir)

    emb = get_embedding()
    docs_dir = PROJECT_ROOT / "data" / "docs"
    uploads_dir = PROJECT_ROOT / "data" / "uploads"
    files = []
    for d in (uploads_dir, docs_dir):
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in (".pdf", ".md", ".txt", ".docx", ".markdown"):
                    files.append(f)
    if not files:
        logger.warning("未找到任何文档文件")
        return {"total": 0, "success": 0, "failed": 0, "written": 0}

    import datetime as _dt

    now_iso = _dt.datetime.now().isoformat(timespec="seconds")
    success = failed = written = 0
    batch_docs, batch_ids = [], []

    def flush():
        nonlocal batch_docs, batch_ids, written
        if not batch_docs:
            return
        texts = [d.page_content for d in batch_docs]
        vecs = emb.embed_documents(texts)
        col.add(
            ids=batch_ids,
            documents=texts,
            metadatas=[d.metadata for d in batch_docs],
            embeddings=vecs,
        )
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
                flush()
        except Exception as e:  # noqa: BLE001
            logger.error("[%d/%d] %s → 失败: %s", i, len(files), f.name, e)
            failed += 1
    flush()

    # 最终 flush：停止写入实例的 system（SharedSystemClient 单例，幂等）
    client._system.stop()
    logger.info("写入实例 chroma system 已停止")
    logger.info("重建完成: %d 个文档, 成功 %d, 失败 %d, 共 %d 块", len(files), success, failed, written)
    return {"total": len(files), "success": success, "failed": failed, "written": written}


def verify_segments() -> bool:
    """落盘自检：检查 hnsw 段目录是否包含二进制文件。"""
    seg_dir_root = PROJECT_ROOT / "vector_db"
    ok = False
    for d in seg_dir_root.iterdir():
        if d.is_dir():
            names = [f.name for f in d.iterdir()]
            if any(n in ("data_level0.bin", "header.bin", "length.bin") for n in names):
                ok = True
                logger.info("段 %s 落盘完整（%d 个文件）", d.name[:8], len(names))
    if not ok:
        logger.error("未检测到 hnsw 二进制段文件——写入未落盘，请勿使用本库！")
    return ok


def main():
    parser = argparse.ArgumentParser(description="向量库重建工具（原生 chromadb 版）")
    parser.add_argument("--kb", default="default", help="知识库 ID（默认 default）")
    parser.add_argument("--skip-verify", action="store_true", help="跳过落盘自检")
    args = parser.parse_args()

    os.chdir(str(PROJECT_ROOT))
    reingest_native(args.kb)
    if not args.skip_verify:
        verify_segments()


if __name__ == "__main__":
    main()
