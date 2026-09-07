"""文档上传与入库接口（v2.3：上传前清洗 + 查重 + 同名覆盖）。

流程：
1. 校验扩展名与权限
2. 保存上传文件到临时目录
3. 提取文本 → 清洗（基础 + 结构）
4. 切片 → 同名覆盖入库（新的覆盖旧的，仅增量修改变化分片）
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from config.settings import settings
from src.auth.deps import get_current_user
from src.auth.rbac import can_access_kb
from src.ingestion.cleaner import clean_documents
from src.ingestion.loader import load_document
from src.ingestion.splitter import split_documents
from src.review.pipeline import process_and_ingest
from src.utils.helpers import file_hash, safe_filename

router = APIRouter(prefix="/api/upload", tags=["文档上传"])

# 允许的扩展名
ALLOWED_EXT = {".pdf", ".md", ".markdown", ".txt", ".docx"}
# 上传大小上限（Nginx 层 client_max_body_size 100m，应用层再兜底）
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.post("", summary="上传文档并入知识库（清洗 + 查重 + 同名覆盖）")
async def upload_document(
    file: UploadFile = File(...),
    kb_id: str = Form("default"),
    user: dict | None = Depends(get_current_user),
) -> dict:
    filename = safe_filename(file.filename or "unnamed")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")
    if not can_access_kb(user, kb_id):
        raise HTTPException(status_code=403, detail=f"无权上传到知识库：{kb_id}")

    # 保存到临时上传目录
    upload_dir = Path(settings.uploads_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / filename
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{len(content) / 1024 / 1024:.1f}MB），上限 50MB",
        )
    target.write_bytes(content)

    def _process() -> dict:
        """OCR/清洗/切片/embedding 入库均为 CPU/IO 密集同步操作，
        统一放入线程池执行，避免阻塞事件循环。"""
        # 计算文件内容 hash（用于查重）
        content_hash = file_hash(content)

        docs = load_document(target, use_ocr=True)
        if not docs:
            raise HTTPException(status_code=400, detail="未能从文档中提取文本内容")

        # 保留清洗前全文（供审核流计算清洗损失率 + 入库前后对比）
        pre_clean_text = "\n\n".join(d.page_content for d in docs if d.page_content)

        # 上传前清洗（基础 + 结构）：去空白/页眉页脚/页码/目录/空表格/合并断行
        docs = clean_documents(docs)
        if not docs:
            raise HTTPException(status_code=400, detail="文档清洗后为空，可能为纯图片或空白文档")

        chunks = split_documents(docs)
        if not chunks:
            raise HTTPException(status_code=400, detail="文档切片后为空")

        # v3.0 入库审核流：AI 打分 → 达标直接入库（同名覆盖/增量不变），低分挂起待人工审
        return process_and_ingest(
            docs,
            chunks,
            content_hash=content_hash,
            kb_id=kb_id,
            filename=filename,
            pre_clean_text=pre_clean_text,
        )

    try:
        result = await run_in_threadpool(_process)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"入库失败: {e}")

    if result.get("skipped"):
        return {
            "ok": True,
            "filename": filename,
            "kb_id": kb_id,
            "skipped": True,
            "message": "文档内容已存在，已自动跳过（不重复入库）",
        }

    review = result.get("review") or {}
    if result.get("pending_review"):
        return {
            "ok": True,
            "filename": filename,
            "kb_id": kb_id,
            "pending_review": True,
            "doc_id": result.get("doc_id"),
            "ai_score": review.get("score"),
            "reasons": review.get("reasons", []),
            "message": review.get("message", "文档已进入人工审核队列，过审后生效"),
        }
    action = "覆盖更新" if result.get("overwritten") else "入库"
    return {
        "ok": True,
        "filename": filename,
        "kb_id": kb_id,
        "chunks": result["ingested"],
        "doc_id": result["doc_id"],
        "overwritten": bool(result.get("overwritten")),
        "ai_score": review.get("score"),
        "message": f"文档已{action}到知识库「{kb_id}」，共 {result['ingested']} 个片段"
        + (f"（AI 打分 {review.get('score')}）" if review.get("score") is not None else ""),
    }
