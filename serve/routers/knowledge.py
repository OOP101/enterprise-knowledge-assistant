"""知识库管理接口：入库目录文档、删除、统计。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from config.settings import settings
from src.auth.deps import get_current_user, require_role
from src.auth.rbac import can_access_kb
from src.ingestion.cleaner import clean_documents
from src.ingestion.embedder import (
    get_document_content,
    list_documents,
    remove_document,
)
from src.ingestion.loader import load_document
from src.ingestion.splitter import split_documents
from src.review.pipeline import process_and_ingest
from src.utils.helpers import file_hash

router = APIRouter(prefix="/api/knowledge", tags=["知识库管理"])

# 支持的扩展名（用于批量入库扫描）
_SUPPORTED_EXT = {".pdf", ".md", ".markdown", ".txt", ".docx"}


class DeleteRequest(BaseModel):
    doc_id: str


@router.post("/ingest", summary="批量入库 docs 目录文档（仅 admin）")
def ingest_directory(
    kb_id: str = "default",
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    """扫描 docs_dir 下所有支持的文件，按文件逐个入库到指定知识库。

    重复内容自动跳过（基于文件 MD5），返回 ingested/skipped 统计。
    """
    docs_dir = Path(settings.docs_dir)
    if not docs_dir.is_dir():
        return {"ok": True, "ingested": 0, "skipped": 0, "kb_id": kb_id, "message": "docs 目录不存在"}

    total_ingested = 0
    total_skipped = 0
    total_pending = 0
    files_done = []

    for path in sorted(docs_dir.glob("**/*")):
        if not path.is_file() or path.suffix.lower() not in _SUPPORTED_EXT:
            continue
        try:
            content = path.read_bytes()
            content_hash = file_hash(content)
            docs = load_document(path, use_ocr=True)
            if not docs:
                continue
            pre_clean_text = "\n\n".join(d.page_content for d in docs if d.page_content)
            docs = clean_documents(docs)
            if not docs:
                continue
            chunks = split_documents(docs)
            if not chunks:
                continue
            # v3.0：批量入库同样走审核流（低分挂起，过审才生效）
            r = process_and_ingest(
                docs,
                chunks,
                content_hash=content_hash,
                kb_id=kb_id,
                filename=path.name,
                pre_clean_text=pre_clean_text,
            )
            if r.get("skipped"):
                total_skipped += 1
            elif r.get("pending_review"):
                total_pending += 1
            else:
                total_ingested += len(chunks)
                files_done.append(path.name)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("入库失败：%s", path)
            continue

    msg = (
        f"入库到知识库「{kb_id}」完成：新入库 {total_ingested} 个片段（{len(files_done)} 文件），"
        f"跳过 {total_skipped} 个重复，待人工审核 {total_pending} 个"
    )
    return {
        "ok": True,
        "kb_id": kb_id,
        "ingested": total_ingested,
        "skipped": total_skipped,
        "pending_review": total_pending,
        "files": files_done,
        "message": msg,
    }


@router.delete("/{doc_id}", summary="删除指定文档（仅 admin）")
def delete_document(
    doc_id: str,
    kb_id: str = Query("default"),
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    ok = remove_document(doc_id, kb_id=kb_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"删除失败或文档不存在: {doc_id}")
    # 联动清理审核记录与原文缓存（若有）
    try:
        from src.review.store import get_review_store

        get_review_store().remove(doc_id)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "doc_id": doc_id, "kb_id": kb_id}


@router.get("/list", summary="列出已入库文档（需登录）")
def list_knowledge(
    kb_id: str = Query("default"),
    user: dict | None = Depends(get_current_user),
) -> dict:
    if not can_access_kb(user, kb_id):
        raise HTTPException(status_code=403, detail=f"无权访问知识库：{kb_id}")
    result = list_documents(kb_id=kb_id)
    return {"ok": True, "kb_id": kb_id, "total": result["total"], "documents": result["documents"]}


@router.get("/doc/{doc_id}", summary="查看文档原文（需登录）")
def view_document(
    doc_id: str,
    kb_id: str = Query("default"),
    user: dict | None = Depends(get_current_user),
) -> dict:
    if not can_access_kb(user, kb_id):
        raise HTTPException(status_code=403, detail=f"无权访问知识库：{kb_id}")
    data = get_document_content(doc_id, kb_id=kb_id)
    if data is None:
        raise HTTPException(status_code=404, detail="文档不存在或内容为空")
    return {"ok": True, **data}


@router.get("/stats", summary="知识库统计（需登录）")
def stats(_: dict | None = Depends(get_current_user)) -> dict:
    """认证开启时需登录；返回值含服务器路径，不对匿名暴露。"""
    return {
        "docs_dir": settings.docs_dir,
        "uploads_dir": settings.uploads_dir,
        "vector_store": settings.vector_store,
        "persist_dir": settings.chroma_persist_dir,
        "embedding_model": settings.embedding_model,
        "llm_model": settings.llm_model,
        "llm_ready": settings.llm_ready,
    }
