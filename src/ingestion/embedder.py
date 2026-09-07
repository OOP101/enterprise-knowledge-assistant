"""向量化与入库。

将切片后的 Document 写入向量库（ChromaDB），支持：
- 按文件内容 hash 去重（跳过重复入库）
- **分片级增量更新**（按 chunk_hash 指纹复用未变化的分片，只增删变更部分，替换 1.0 整文档重建）
- 删除旧版本（doc_id 维度）
- Milvus 生产切换预留
- 记录首次导入时间 created_at 与最近更新时间 updated_at（ISO 8601）
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from config.settings import settings
from src.models.embedding import get_embedding
from src.retrieval.hybrid_search import invalidate_corpus
from src.utils.helpers import new_doc_id

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """返回当前本地时间，秒级 ISO 8601 字符串（与 auth/users.py 保持一致风格）。"""
    return _dt.datetime.now().isoformat(timespec="seconds")


def collection_name_for_kb(kb_id: str | None = None) -> str | None:
    """按知识库 ID 解析 Chroma collection 名（与 Container._collection_name 保持一致）。

    - 空 / default → 返回 None（由 get_vectorstore 使用 settings.chroma_collection 默认值）
    - 其他 → ``enterprise_knowledge_{kb_id}``
    """
    if not kb_id or kb_id in ("", "default"):
        return None
    return f"{settings.chroma_collection}_{kb_id}"


def repair_chroma_segments() -> int:
    """启动自愈：清理"半初始化"的向量段目录（chromadb 1.3+/1.5 在部分 Windows
    环境下后台压缩器不把段数据文件落盘，只留下仅含 index_metadata.pickle 的
    段目录，导致下次打开报 "Error loading hnsw index"）。

    数据本体在 chroma.sqlite3 的 embeddings_queue 里并未丢失；删除残缺段目录后，
    chroma 打开集合时会自动从队列回填重建段。返回清理的目录数（仅诊断用）。
    """
    import os
    import shutil
    import sqlite3

    persist = Path(settings.chroma_persist_dir)
    db_file = persist / "chroma.sqlite3"
    if not db_file.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_file))
        seg_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM segments WHERE type LIKE '%vector%'"
            )
        ]
        conn.close()
    except Exception:  # noqa: BLE001
        return 0

    removed = 0
    for sid in seg_ids:
        seg_dir = persist / sid
        if not seg_dir.is_dir():
            continue
        files = list(seg_dir.iterdir())
        # 段目录没有任何 .bin 数据文件 → 半初始化，删除以触发回填
        if files and not any(f.name.endswith(".bin") for f in files):
            try:
                shutil.rmtree(seg_dir)
                removed += 1
                logger.warning("已清理半初始化向量段目录（将从写入队列自动回填）：%s", sid)
            except Exception:  # noqa: BLE001
                pass
    return removed


def get_vectorstore(**kwargs: Any) -> Any:
    """返回全局向量库实例（带持久化）。

    优先级：Milvus（生产）> ChromaDB（默认开发）> 内存向量库（无依赖兜底）。
    ``collection_name`` 允许由调用方（如 Container 的多知识库隔离）覆盖。
    ``kb_id`` 便捷参数：按知识库解析 collection 名（与 container 规则一致）。
    """
    kb_id = kwargs.pop("kb_id", None)
    if kb_id:
        kwargs["collection_name"] = kwargs.get("collection_name") or collection_name_for_kb(kb_id)
    vector_store = kwargs.pop("vector_store", settings.vector_store).lower()
    collection_name = kwargs.pop("collection_name", None)

    if vector_store == "milvus":
        try:
            from langchain_milvus import Milvus  # type: ignore

            return Milvus(
                embedding_function=get_embedding(),
                connection_args={"uri": settings.milvus_uri},
                collection_name=collection_name or settings.milvus_collection,
                auto_id=True,
                drop_old=False,
                **kwargs,
            )
        except ImportError:
            logger.warning("未安装 langchain-milvus，回退到 ChromaDB")

    try:
        from langchain_chroma import Chroma

        return Chroma(
            embedding_function=get_embedding(),
            collection_name=collection_name or settings.chroma_collection,
            persist_directory=settings.chroma_persist_dir,
            **kwargs,
        )
    except ImportError:
        logger.warning("未安装 langchain-chroma，回退到内存向量库（不持久化）")
        from langchain_community.vectorstores import InMemoryVectorStore

        return InMemoryVectorStore(embedding=get_embedding())


def _chunk_hash(text: str) -> str:
    """计算单个分片的稳定内容指纹（md5）。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _doc_exists_by_hash(vs: Any, content_hash: str) -> bool:
    """检查向量库中是否已存在相同内容 hash 的文档。"""
    try:
        collection = vs._collection
        found = collection.get(where={"content_hash": content_hash}, limit=1)
        return bool(found.get("ids"))
    except Exception:  # noqa: BLE001
        return False


def _find_doc_id_by_filename(vs: Any, filename: str) -> str | None:
    """按文件名（source 的 basename）在向量库中查找已入库的 doc_id。

    用于「同名文档覆盖」：上传同名文件时定位旧 doc_id，复用其做分片级增量，
    实现只修改变化分片、保留未变化分片。找不到时返回 None。
    """
    if not filename:
        return None
    try:
        collection = vs._collection
        # 先按 source 精确匹配（source 存的是完整路径），再兜底按文件名匹配
        found = collection.get(where={"source": filename}, include=["metadatas"], limit=1)
        ids = found.get("ids", []) or []
        if ids:
            metas = found.get("metadatas", []) or []
            return metas[0].get("doc_id") if metas else None
    except Exception:  # noqa: BLE001
        pass

    # 兜底：遍历元数据匹配 basename（source 可能是完整路径）
    try:
        collection = vs._collection
        data = collection.get(include=["metadatas"], limit=min(collection.count(), 20000))
        ids = data.get("ids", []) or []
        metas = data.get("metadatas", []) or []
        for mid, meta in zip(ids, metas):
            meta = meta or {}
            src = str(meta.get("source", ""))
            if src.split("/")[-1].split("\\")[-1] == filename:
                return meta.get("doc_id")
    except Exception:  # noqa: BLE001
        pass
    return None


def _get_chunk_hashes_by_doc_id(vs: Any, doc_id: str) -> dict[str, str]:
    """按 doc_id 返回 {chunk_id: chunk_hash}（用于分片级增量比对）。

    内存向量库等无 _collection 的实现返回空 dict（退化为整文档重建）。
    """
    try:
        collection = vs._collection
        found = collection.get(
            where={"doc_id": doc_id}, include=["metadatas"]
        )
        ids = found.get("ids", []) or []
        metas = found.get("metadatas", []) or []
        result: dict[str, str] = {}
        for cid, meta in zip(ids, metas):
            meta = meta or {}
            if meta.get("chunk_hash"):
                result[cid] = meta["chunk_hash"]
        return result
    except Exception:  # noqa: BLE001
        return {}


def _delete_ids(vs: Any, ids: list[str]) -> None:
    """按 id 列表删除分片（兼容无 _collection 的实现：整库清空降级）。"""
    if not ids:
        return
    try:
        vs._collection.delete(ids=ids)
        logger.info("删除 %d 个旧分片", len(ids))
    except AttributeError:
        # 内存向量库等无 _collection 的实现，尝试整体删除（无法按 where 过滤）
        try:
            vs.delete(ids=ids)
        except Exception as e:  # noqa: BLE001
            logger.debug("分片删除跳过（%s）", e)


def ingest_documents(
    docs: list[Document],
    doc_id: str | None = None,
    content_hash: str | None = None,
    kb_id: str = "default",
    filename: str | None = None,
) -> dict:
    """将一批 Document 写入向量库（分片级增量）。

    Args:
        docs: 切片后的文档块。
        doc_id: 文档 ID；未提供时按 filename 在库中查找旧 doc_id（同名覆盖），
            仍找不到才生成新 ID。相同 doc_id 再次入库时，仅删除被替换/移除的
            分片、仅新增变化的分片（未变化分片复用，避免整文档重建）。
        content_hash: 文档内容 MD5；提供时先查重。若与库中已有文档内容完全相同
            （且非同名覆盖场景），跳过入库防止重复。
        kb_id: 目标知识库 ID（对应独立 collection），默认 default 库。
        filename: 上传文件名（用于同名覆盖定位旧 doc_id）。传入后优先按文件名
            匹配旧 doc_id 做增量覆盖，实现「新的覆盖旧的、只改变化分片」。

    Returns:
        统计信息 {ingested, doc_id, source, skipped, overwritten}
    """
    if not docs:
        return {"ingested": 0, "doc_id": doc_id, "source": None, "skipped": False, "overwritten": False}

    source = docs[0].metadata.get("source", "unknown")
    import time as _time

    _t0 = _time.perf_counter()

    vs = get_vectorstore(kb_id=kb_id)

    # 同名覆盖定位：优先按文件名找旧 doc_id（新的覆盖旧的）
    overwritten = False
    if doc_id is None and filename:
        old_id = _find_doc_id_by_filename(vs, filename)
        if old_id:
            doc_id = old_id
            overwritten = True

    # 内容去重（基于 content_hash，整文件级）：仅在非覆盖场景下跳过。
    # 覆盖场景即使内容 hash 相同也要重走增量，保证 source/metadata 更新为最新。
    if content_hash and _doc_exists_by_hash(vs, content_hash) and not overwritten:
        logger.info("跳过重复入库（content_hash=%s, source=%s）", content_hash[:8], source)
        return {"ingested": 0, "doc_id": None, "source": source, "skipped": True, "overwritten": False}

    if doc_id is None:
        doc_id = new_doc_id()

    # 为每个块补充元数据（含分片内容指纹，供增量比对）
    # 时间字段：同名覆盖（overwritten）保留原 created_at，仅刷新 updated_at；
    # 新建场景 created_at = updated_at = 当前时间。读取阶段通过任一分片即可拿到。
    now_iso = _now_iso()
    # 先读出已存在的 created_at（同名覆盖时复用首次导入时间）
    existing_created_at: str | None = None
    if overwritten:
        try:
            found = vs._collection.get(where={"doc_id": doc_id}, include=["metadatas"], limit=1)
            metas = (found or {}).get("metadatas") or []
            if metas and metas[0]:
                existing_created_at = metas[0].get("created_at")
        except Exception:  # noqa: BLE001
            existing_created_at = None
    final_created_at = existing_created_at or now_iso

    chunk_ids = [f"{doc_id}-{i}" for i in range(len(docs))]
    for i, d in enumerate(docs):
        d.metadata["doc_id"] = doc_id
        d.metadata["chunk_index"] = i
        d.metadata.setdefault("source", source)
        d.metadata["chunk_hash"] = _chunk_hash(d.page_content)
        if content_hash:
            d.metadata["content_hash"] = content_hash
        d.metadata["created_at"] = final_created_at
        d.metadata["updated_at"] = now_iso

    # 分片级增量：复用未变化分片，只增删变更部分
    existing = _get_chunk_hashes_by_doc_id(vs, doc_id)
    existing_ids = set(existing)
    new_ids = set(chunk_ids)

    to_add: list[tuple[Document, str]] = []
    stale_ids: list[str] = []
    for doc, cid in zip(docs, chunk_ids):
        if cid in existing and existing[cid] == doc.metadata["chunk_hash"]:
            continue  # 内容未变化，复用已有分片
        to_add.append((doc, cid))
        if cid in existing:
            stale_ids.append(cid)
    stale_ids.extend(sorted(existing_ids - new_ids))  # 新文档中已不存在的旧分片

    _delete_ids(vs, stale_ids)

    if to_add:
        add_docs = [d for d, _ in to_add]
        add_ids = [cid for _, cid in to_add]
        vs.add_documents(add_docs, ids=add_ids)

    # 语料已变化：使该库的全量 BM25 缓存 + 查询缓存失效
    if to_add or stale_ids:
        invalidate_corpus(kb_id)
        # v1.5：文档变更后清空查询缓存
        try:
            from src.retrieval.cache import get_query_cache
            get_query_cache().invalidate_kb(kb_id)
        except Exception:  # noqa: BLE001
            pass

    logger.info(
        "入库完成：doc_id=%s source=%s 共 %d 块（新增 %d，复用 %d，删除 %d）耗时 %.2fs",
        doc_id, source, len(docs), len(to_add), len(docs) - len(to_add), len(stale_ids),
        _time.perf_counter() - _t0,
    )
    return {
        "ingested": len(to_add),
        "doc_id": doc_id,
        "source": source,
        "skipped": False,
        "overwritten": overwritten,
    }


def _delete_by_doc_id(vs: Any, doc_id: str) -> None:
    """按 doc_id 删除所有对应块。

    兼容 langchain_chroma 1.x：先通过底层 collection 查询 ids，再按 ids 删除。
    """
    try:
        collection = vs._collection
        found = collection.get(where={"doc_id": doc_id})
        ids = found.get("ids", [])
        if ids:
            collection.delete(ids=ids)
            logger.info("删除 doc_id=%s 的 %d 个旧块", doc_id, len(ids))
    except AttributeError:
        # 内存向量库等无 _collection 的实现，尝试直接 delete
        try:
            vs.delete(ids=None)
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        logger.debug("增量删除跳过（可能无历史）：%s", e)


def remove_document(doc_id: str, kb_id: str = "default") -> bool:
    """按 doc_id 删除文档（默认 default 库，可指定知识库）。"""
    vs = get_vectorstore(kb_id=kb_id)
    try:
        _delete_by_doc_id(vs, doc_id)
        invalidate_corpus(kb_id)
        # v1.5：删除文档后清空查询缓存
        try:
            from src.retrieval.cache import get_query_cache
            get_query_cache().invalidate_kb(kb_id)
        except Exception:  # noqa: BLE001
            pass
        logger.info("已删除文档：%s（kb=%s）", doc_id, kb_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("删除文档失败：%s，原因：%s", doc_id, e)
        return False


def get_document_content(doc_id: str, kb_id: str = "default") -> dict | None:
    """按 doc_id 聚合原文内容（用于前端「查看原文」，可指定知识库）。

    按 chunk_index 顺序拼接所有分片 page_content，还原入库文本。
    不依赖原始文件是否存在（OCR 后文本也在向量库中）。
    """
    vs = get_vectorstore(kb_id=kb_id)
    try:
        collection = vs._collection
        found = collection.get(
            where={"doc_id": doc_id}, include=["metadatas", "documents"]
        )
        ids = found.get("ids", []) or []
        metas = found.get("metadatas", []) or []
        docs = found.get("documents", []) or []
    except AttributeError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.exception("读取文档内容失败：%s", e)
        return None

    if not ids:
        return None

    chunks: list[tuple[int, str]] = []
    source = "unknown"
    for meta, doc in zip(metas, docs):
        meta = meta or {}
        idx = meta.get("chunk_index", 0)
        chunks.append((idx, doc or ""))
        source = meta.get("source", source)

    chunks.sort(key=lambda x: x[0])
    content = "\n\n".join(c for _, c in chunks)
    return {"doc_id": doc_id, "source": source, "content": content}


def list_documents(limit: int = 500, kb_id: str = "default") -> dict:
    """列出已入库文档（按 doc_id 聚合，含分块数，可指定知识库）。

    limit: 最多返回的文档条数（默认 500 防止大库卡顿）。
    返回 ``{"documents": [...], "total": 真实文档总数}``，total 为聚合后的
    完整文档数（不受 limit 截断影响），供前端正确分页展示。
    """
    vs = get_vectorstore(kb_id=kb_id)
    try:
        collection = vs._collection
        total_chunks = collection.count()
        # 仅拉取元数据，不拉 embeddings，按 source 去重聚合时控制总条数
        data = collection.get(include=["metadatas"], limit=min(total_chunks, 20000))
        ids = data.get("ids", [])
        metas = data.get("metadatas", []) or []
    except AttributeError:
        return {"documents": [], "total": 0}
    except Exception as e:  # noqa: BLE001
        logger.exception("列出文档失败：%s", e)
        return {"documents": [], "total": 0}

    agg: dict[str, dict] = {}
    for mid, meta in zip(ids, metas):
        meta = meta or {}
        doc_id = meta.get("doc_id", mid)
        source = meta.get("source", "unknown")
        created_at = meta.get("created_at")
        updated_at = meta.get("updated_at")
        if doc_id not in agg:
            agg[doc_id] = {
                "doc_id": doc_id,
                "source": source,
                "chunks": 0,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        else:
            # 跨分片合并时间：created_at 取最早（首次入库），updated_at 取最近一次
            if created_at and (not agg[doc_id]["created_at"] or created_at < agg[doc_id]["created_at"]):
                agg[doc_id]["created_at"] = created_at
            if updated_at and (not agg[doc_id]["updated_at"] or updated_at > agg[doc_id]["updated_at"]):
                agg[doc_id]["updated_at"] = updated_at
        agg[doc_id]["chunks"] += 1

    # 默认按「文件名编号」自然排序（如 001、002…056），便于按目录顺序查看。
    # 排序键：先按前导数字（无编号的排最后，按名称），再按源文件名保证稳定。
    import re as _re

    def _sort_key(item: dict) -> tuple:
        source = item.get("source", "")
        name = str(source).split("/")[-1].split("\\")[-1]
        m = _re.match(r"^\s*(\d+)", name)
        if m:
            return (0, int(m.group(1)), name)
        return (1, 0, name)

    result = sorted(agg.values(), key=_sort_key)
    return {"documents": result[:limit], "total": len(result)}
