"""审核存储与文档状态机（v3.0）。

- ReviewStore：JSON 持久化（原子写 + 线程安全，风格与 KnowledgeBaseManager 一致），
  记录每份文档的审核状态、AI 打分、人工打分与标注。
- 状态机：pending_score → pending_review → approved / rejected
- 双层存储：tier = raw（原始库，仅存档）/ highlight（亮点库，参与检索）
- 原文缓存：data/raw_docs/{doc_id}.raw.txt（清洗前）与 .clean.txt（清洗后），
  供「入库前后对比」与过审后重切片入库。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path
from typing import Any

from config.settings import settings

REVIEW_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "review_store.json"

# 合法状态与流转
STATUSES = ("pending_score", "pending_review", "approved", "rejected")


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


class ReviewStore:
    """文档审核记录存储（JSON 文件，线程安全）。"""

    def __init__(
        self,
        path: Path | None = None,
        raw_dir: Path | None = None,
    ) -> None:
        self._path = path or REVIEW_FILE
        self._raw_dir = raw_dir or Path(settings.raw_docs_dir)
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            if self._path.exists():
                self._records = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self._records = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    # ---------- 原文缓存（双层存储的 raw 层）----------

    def save_texts(self, doc_id: str, raw_text: str, clean_text: str) -> None:
        """保存清洗前/清洗后全文缓存。"""
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        (self._raw_dir / f"{doc_id}.raw.txt").write_text(raw_text or "", encoding="utf-8")
        (self._raw_dir / f"{doc_id}.clean.txt").write_text(clean_text or "", encoding="utf-8")

    def load_text(self, doc_id: str, kind: str = "clean") -> str | None:
        """读取原文缓存。kind: raw | clean。"""
        p = self._raw_dir / f"{doc_id}.{kind}.txt"
        try:
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return None

    def delete_texts(self, doc_id: str) -> None:
        for kind in ("raw", "clean"):
            p = self._raw_dir / f"{doc_id}.{kind}.txt"
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 状态机 ----------

    def submit(
        self,
        doc_id: str,
        *,
        kb_id: str,
        filename: str,
        status: str,
        score_info: dict,
        raw_text: str = "",
        clean_text: str = "",
    ) -> dict:
        """登记一份文档的打分结果与初始状态（pending_review 或 approved）。"""
        if status not in ("pending_review", "approved"):
            raise ValueError(f"submit 不允许的初始状态: {status}")
        now = _now_iso()
        record = {
            "doc_id": doc_id,
            "kb_id": kb_id,
            "filename": filename,
            "status": status,
            "tier": "highlight" if status == "approved" else "raw",
            "ai_score": score_info.get("score"),
            "score_reasons": score_info.get("reasons", []),
            "review_score": None,
            "review_comment": None,
            "annotations": [],
            "rejected_reason": None,
            "raw_text_path": str(self._raw_dir / f"{doc_id}.raw.txt"),
            "clean_text_path": str(self._raw_dir / f"{doc_id}.clean.txt"),
            "created_at": now,
            "updated_at": now,
        }
        self.save_texts(doc_id, raw_text, clean_text)
        with self._lock:
            self._records[doc_id] = record
            self._save()
        return dict(record)

    def get(self, doc_id: str) -> dict | None:
        with self._lock:
            return dict(self._records[doc_id]) if doc_id in self._records else None

    def list_by_status(self, status: str, kb_id: str | None = None) -> list[dict]:
        with self._lock:
            records = [dict(r) for r in self._records.values() if r["status"] == status]
        if kb_id:
            records = [r for r in records if r.get("kb_id") == kb_id]
        return records

    def queue(self, kb_id: str | None = None) -> list[dict]:
        """待审核队列：按 AI 分数升序（最差的排最前）。"""
        records = self.list_by_status("pending_review", kb_id=kb_id)
        return sorted(records, key=lambda r: (r.get("ai_score") if r.get("ai_score") is not None else 0))

    def counts(self) -> dict:
        with self._lock:
            result = {s: 0 for s in STATUSES}
            for r in self._records.values():
                result[r["status"]] = result.get(r["status"], 0) + 1
        return result

    def _update(self, doc_id: str, mutate: Any) -> dict | None:
        with self._lock:
            record = self._records.get(doc_id)
            if record is None:
                return None
            mutate(record)
            record["updated_at"] = _now_iso()
            self._save()
            return dict(record)

    def annotate(self, doc_id: str, chunk_index: int, note: str, by: str | None = None) -> dict | None:
        """标注关键分片（chunk_index 以审核时切片顺序为准）。"""
        if note is None or not str(note).strip():
            raise ValueError("标注内容不能为空")

        def _mutate(r: dict) -> None:
            for a in r["annotations"]:
                if a["chunk_index"] == chunk_index:
                    a["note"] = str(note).strip()
                    a["by"] = by
                    a["at"] = _now_iso()
                    return
            r["annotations"].append(
                {"chunk_index": chunk_index, "note": str(note).strip(), "by": by, "at": _now_iso()}
            )

        return self._update(doc_id, _mutate)

    def remove_annotation(self, doc_id: str, chunk_index: int) -> dict | None:
        def _mutate(r: dict) -> None:
            r["annotations"] = [a for a in r["annotations"] if a["chunk_index"] != chunk_index]

        return self._update(doc_id, _mutate)

    def approve(
        self,
        doc_id: str,
        review_score: int | None = None,
        comment: str | None = None,
        final_doc_id: str | None = None,
        by: str | None = None,
    ) -> dict | None:
        """人工通过：状态 → approved，tier → highlight。

        final_doc_id: 过审重切片入库后由 ingest 返回的真实 doc_id
        （同名覆盖时可能与暂存 doc_id 不同），此处同步更新。
        """

        def _mutate(r: dict) -> None:
            r["status"] = "approved"
            r["tier"] = "highlight"
            if review_score is not None:
                r["review_score"] = review_score
            if comment:
                r["review_comment"] = comment
            if by:
                r["reviewed_by"] = by
            if final_doc_id:
                r["doc_id"] = final_doc_id

        return self._update(doc_id, _mutate)

    def reject(self, doc_id: str, reason: str = "", by: str | None = None) -> dict | None:
        """退回：状态 → rejected，tier → raw。已生效文档由调用方负责从索引移除。"""

        def _mutate(r: dict) -> None:
            r["status"] = "rejected"
            r["tier"] = "raw"
            r["rejected_reason"] = reason or None
            if by:
                r["reviewed_by"] = by

        return self._update(doc_id, _mutate)

    def remove(self, doc_id: str) -> bool:
        """删除审核记录（文档被删除时联动清理）。"""
        with self._lock:
            if doc_id not in self._records:
                return False
            del self._records[doc_id]
            self._save()
        self.delete_texts(doc_id)
        return True


_store: ReviewStore | None = None


def get_review_store() -> ReviewStore:
    global _store
    if _store is None:
        _store = ReviewStore()
    return _store
