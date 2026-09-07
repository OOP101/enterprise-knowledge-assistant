"""人工审核接口（v3.0 入库审核流）。

- GET  /api/review/queue            待审核队列（按 AI 分数升序）
- GET  /api/review/{doc_id}/diff    入库前后对比（清洗前原文 vs 清洗后切片）
- POST /api/review/{doc_id}/approve 人工通过（可带打分/评语）→ 重切片入库生效
- POST /api/review/{doc_id}/reject  人工退回（已生效文档同步移出索引）
- POST /api/review/{doc_id}/annotate 标注关键分片（过审后检索加权）
- GET  /api/review/stats            审核状态统计

权限：与知识库管理接口一致（认证开启时需 admin）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from config.settings import settings
from src.auth.deps import require_role
from src.review.pipeline import approve_document, reject_document, review_chunks
from src.review.store import get_review_store

router = APIRouter(prefix="/api/review", tags=["入库审核"])

# diff 单次返回的文本上限（字符），防止超大文档撑爆响应
_MAX_TEXT_CHARS = 40000
_MAX_CHUNKS = 500


class ReviewAction(BaseModel):
    review_score: int | None = None
    comment: str | None = None
    by: str | None = None


class RejectAction(BaseModel):
    reason: str = ""
    by: str | None = None


class AnnotateAction(BaseModel):
    chunk_index: int
    note: str
    by: str | None = None


@router.get("/queue", summary="待审核队列（按 AI 分数升序，最差在前）")
def review_queue(
    kb_id: str | None = Query(None),
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    store = get_review_store()
    return {
        "ok": True,
        "kb_id": kb_id,
        "threshold": settings.review_score_threshold,
        "mode": settings.review_mode,
        "counts": store.counts(),
        "queue": store.queue(kb_id=kb_id),
    }


@router.get("/stats", summary="审核状态统计")
def review_stats(_: dict | None = Depends(require_role("admin"))) -> dict:
    return {"ok": True, "counts": get_review_store().counts()}


@router.get("/{doc_id}/diff", summary="入库前后对比：清洗前原文 vs 清洗后切片")
def review_diff(
    doc_id: str,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    store = get_review_store()
    record = store.get(doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"审核记录不存在: {doc_id}")

    raw_text = store.load_text(doc_id, kind="raw") or ""
    clean_text = store.load_text(doc_id, kind="clean") or ""

    chunks = review_chunks(clean_text, record.get("filename", ""))
    annotated = {a["chunk_index"]: a for a in record.get("annotations", [])}
    chunk_list = [
        {
            "index": i,
            "text": c.page_content,
            "highlight": bool(annotated.get(i)),
            "note": annotated.get(i, {}).get("note"),
        }
        for i, c in enumerate(chunks[:_MAX_CHUNKS])
    ]

    pre_chars = len(raw_text)
    post_chars = len(clean_text)
    loss = round(1 - post_chars / pre_chars, 4) if pre_chars > 0 else None
    return {
        "ok": True,
        "record": record,
        "pre_clean_chars": pre_chars,
        "post_clean_chars": post_chars,
        "clean_loss_ratio": loss,
        "raw_text": raw_text[:_MAX_TEXT_CHARS],
        "clean_text": clean_text[:_MAX_TEXT_CHARS],
        "truncated": len(raw_text) > _MAX_TEXT_CHARS or len(clean_text) > _MAX_TEXT_CHARS,
        "chunk_count": len(chunks),
        "chunks": chunk_list,
    }


@router.post("/{doc_id}/approve", summary="人工通过：重切片入库并生效（晋升亮点层）")
def approve(
    doc_id: str,
    action: ReviewAction | None = None,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    action = action or ReviewAction()
    if action.review_score is not None and not (0 <= action.review_score <= 100):
        raise HTTPException(status_code=400, detail="review_score 需在 0-100 之间")
    try:
        result = approve_document(
            doc_id,
            review_score=action.review_score,
            comment=action.comment,
            by=action.by,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"过审入库失败: {e}")
    return {"ok": True, **result}


@router.post("/{doc_id}/reject", summary="人工退回（已生效文档同步移出索引）")
def reject(
    doc_id: str,
    action: RejectAction | None = None,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    action = action or RejectAction()
    try:
        return reject_document(doc_id, reason=action.reason, by=action.by)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{doc_id}/annotate", summary="标注关键分片（过审后该分片检索加权）")
def annotate(
    doc_id: str,
    action: AnnotateAction,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    store = get_review_store()
    try:
        record = store.annotate(doc_id, action.chunk_index, action.note, by=action.by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if record is None:
        raise HTTPException(status_code=404, detail=f"审核记录不存在: {doc_id}")
    return {"ok": True, "record": record}


@router.delete("/{doc_id}", summary="删除审核记录与原文缓存（仅 admin）")
def delete_review(
    doc_id: str,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    if not get_review_store().remove(doc_id):
        raise HTTPException(status_code=404, detail=f"审核记录不存在: {doc_id}")
    return {"ok": True, "doc_id": doc_id}
