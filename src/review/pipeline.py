"""入库审核流水线（v3.0）：打分 → 阈值判定 → 直接入库 / 推送人工审。

规则（需求确认 2026-09-05）：
- 审核是入库硬门槛：清洗筛选通过 + 过审才可进检索；
- AI 全量打分，低于阈值才推送人工审（review_mode=threshold），
  可切 all（全量人工审）/ off（仅打分不拦截）；
- 双层存储：过审文档的切片进向量索引（亮点层），
  待审/退回文档仅保留原文缓存（原始库 raw 层），不占索引资源。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.documents import Document

from config.settings import settings
from src.review.scorer import score_document
from src.review.store import ReviewStore, get_review_store

logger = logging.getLogger(__name__)


def _join_text(docs: list[Document]) -> str:
    return "\n\n".join(d.page_content for d in docs if d.page_content)


def process_and_ingest(
    docs: list[Document],
    chunks: list[Document],
    *,
    content_hash: str | None = None,
    kb_id: str = "default",
    filename: str | None = None,
    pre_clean_text: str | None = None,
    store: ReviewStore | None = None,
) -> dict:
    """清洗后的文档：打分 → 阈值判定 → 直接入库或挂起待审。

    Args:
        docs: 清洗后的 Document 列表（未切片）。
        chunks: split_documents 切片结果。
        content_hash: 内容 MD5（透传给 ingest 查重）。
        kb_id: 目标知识库。
        filename: 文件名（同名覆盖定位用）。
        pre_clean_text: 清洗前全文（用于计算清洗损失率，可缺省）。
        store: 审核存储（缺省用全局单例，测试可注入临时实例）。

    Returns:
        ingest_documents 的结果 dict，额外附加 "review" 块：
        {"status": approved|pending_review|bypassed, "score": int,
         "reasons": [...], "message": str}
    """
    store = store or get_review_store()

    # 审核关闭：与 2.x 行为完全一致
    if not settings.review_enabled or settings.review_mode == "disabled":
        result = ingest_documents(
            chunks, content_hash=content_hash, kb_id=kb_id, filename=filename
        )
        result["review"] = {"status": "bypassed", "score": None, "reasons": []}
        return result

    clean_text = _join_text(docs)
    pre_clean_chars = len(pre_clean_text) if pre_clean_text else None
    score_info = score_document(clean_text, pre_clean_chars=pre_clean_chars, source=filename or "")
    score = score_info["score"]

    # 是否推送人工审：threshold 模式看分数，all 模式全量推送
    if settings.review_mode == "all":
        need_review = True
    elif settings.review_mode == "off":
        need_review = False
    else:
        need_review = score < settings.review_score_threshold

    if not need_review:
        result = ingest_documents(
            chunks, content_hash=content_hash, kb_id=kb_id, filename=filename
        )
        if result.get("skipped"):
            # 内容重复被跳过：无需登记审核记录
            result["review"] = {
                "status": "skipped_duplicate",
                "score": score,
                "reasons": score_info["reasons"],
            }
            return result
        doc_id = result.get("doc_id")
        store.submit(
            doc_id,
            kb_id=kb_id,
            filename=filename or "",
            status="approved",
            score_info=score_info,
            raw_text=pre_clean_text or "",
            clean_text=clean_text,
        )
        result["review"] = {
            "status": "approved",
            "score": score,
            "reasons": score_info["reasons"],
            "message": f"AI 打分 {score}（阈值 {settings.review_score_threshold}），已直接生效",
        }
        return result

    # 低分/全量模式：不进向量索引，挂起待人工审（原始库 raw 层留底）
    provisional_doc_id = new_doc_id()
    record = store.submit(
        provisional_doc_id,
        kb_id=kb_id,
        filename=filename or "",
        status="pending_review",
        score_info=score_info,
        raw_text=pre_clean_text or "",
        clean_text=clean_text,
    )
    logger.info(
        "文档进入人工审核队列：%s score=%s（阈值 %s）",
        filename, score, settings.review_score_threshold,
    )
    return {
        "ingested": 0,
        "doc_id": provisional_doc_id,
        "source": None,
        "skipped": False,
        "overwritten": False,
        "pending_review": True,
        "review": {
            "status": "pending_review",
            "score": score,
            "reasons": score_info["reasons"],
            "message": (
                f"AI 打分 {score} 低于阈值 {settings.review_score_threshold}，"
                "已进入人工审核队列（过审后才会生效）"
            ),
        },
    }


def review_chunks(clean_text: str, filename: str) -> list[Document]:
    """审核切片：以审核存储的 clean_text 重新切片。

    diff 展示与过审入库使用同一函数，保证 chunk_index 一致，
    人工标注按 chunk_index 落到对应分片元数据。
    """
    return split_documents([Document(page_content=clean_text, metadata={"source": filename or ""})])


def apply_annotations(chunks: list[Document], annotations: list[dict]) -> int:
    """把人工标注写入分片元数据（highlight + annotation）。返回命中分片数。"""
    hits = 0
    for a in annotations or []:
        idx = a.get("chunk_index")
        if idx is None or not (0 <= idx < len(chunks)):
            continue
        chunks[idx].metadata["highlight"] = True
        chunks[idx].metadata["annotation"] = str(a.get("note", ""))[:500]
        hits += 1
    return hits


def approve_document(
    doc_id: str,
    review_score: int | None = None,
    comment: str | None = None,
    by: str | None = None,
    store: ReviewStore | None = None,
) -> dict:
    """人工通过：从原文缓存重切片 → 带标注入库（晋升亮点层）→ 状态 approved。"""
    store = store or get_review_store()
    record = store.get(doc_id)
    if record is None:
        raise LookupError(f"审核记录不存在: {doc_id}")
    if record["status"] == "approved":
        raise ValueError("该文档已是生效状态")
    clean_text = store.load_text(doc_id, kind="clean")
    if not clean_text:
        raise ValueError("原文缓存缺失，无法入库（请重新上传）")

    chunks = review_chunks(clean_text, record.get("filename", ""))
    apply_annotations(chunks, record.get("annotations", []))
    result = ingest_documents(
        chunks, kb_id=record.get("kb_id", "default"), filename=record.get("filename") or None
    )
    updated = store.approve(
        doc_id,
        review_score=review_score,
        comment=comment,
        final_doc_id=result.get("doc_id"),
        by=by,
    )
    result["review"] = {"status": "approved", "record": updated}
    return result


def reject_document(
    doc_id: str,
    reason: str = "",
    by: str | None = None,
    store: ReviewStore | None = None,
) -> dict:
    """人工退回：状态 → rejected。已生效文档同步从索引移除（降级回原始层）。"""
    store = store or get_review_store()
    record = store.get(doc_id)
    if record is None:
        raise LookupError(f"审核记录不存在: {doc_id}")

    purged = False
    if record["status"] == "approved" and record.get("doc_id"):
        purged = remove_document(record["doc_id"], kb_id=record.get("kb_id", "default"))

    updated = store.reject(doc_id, reason=reason, by=by)
    return {"ok": True, "purged_from_index": purged, "record": updated}


# 延迟导入：避免循环依赖（embedder/hybrid_search 不依赖 review）
def ingest_documents(*args: Any, **kwargs: Any) -> dict:  # noqa: ANN401
    from src.ingestion.embedder import ingest_documents as _ingest

    return _ingest(*args, **kwargs)


def remove_document(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
    from src.ingestion.embedder import remove_document as _remove

    return _remove(*args, **kwargs)


def split_documents(*args: Any, **kwargs: Any) -> list[Document]:  # noqa: ANN401
    from src.ingestion.splitter import split_documents as _split

    return _split(*args, **kwargs)


def new_doc_id() -> str:
    from src.utils.helpers import new_doc_id as _new

    return _new()
