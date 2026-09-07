"""用户反馈收集模块。

记录用户对回答的评分，形成"低分回答 → 标记优化"的闭环。
项目文档 v1.5 规划：「用户反馈闭环（低分回答自动标记优化）」
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

FEEDBACK_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "feedback"
FEEDBACK_FILE = FEEDBACK_DIR / "feedback.jsonl"

# 低分阈值（≤2 分标记为待优化）
LOW_SCORE_THRESHOLD = 2


class FeedbackStore:
    """用户反馈存储（JSONL 持久化）。"""

    def __init__(self, file_path: Path | None = None) -> None:
        self._file = file_path or FEEDBACK_FILE
        self._lock = Lock()

    def record(
        self,
        question: str,
        answer: str,
        score: int,
        sources: list | None = None,
        kb_id: str = "default",
        comment: str = "",
    ) -> dict:
        """记录一条用户反馈。

        Args:
            question: 用户问题
            answer: 系统回答
            score: 评分 1-5（5 最满意，1 最不满意）
            sources: 引用来源
            kb_id: 知识库 ID
            comment: 用户文字反馈

        Returns:
            记录的反馈条目（含 ID 和时间戳）
        """
        import uuid

        entry = {
            "id": uuid.uuid4().hex[:12],
            "timestamp": time.time(),
            "question": question,
            "answer": answer,
            "score": score,
            "sources": sources or [],
            "kb_id": kb_id,
            "comment": comment,
            "status": "pending" if score <= LOW_SCORE_THRESHOLD else "resolved",
        }

        with self._lock:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        if score <= LOW_SCORE_THRESHOLD:
            logger.info(
                "低分反馈标记（score=%d）：%s → 待优化",
                score, question[:50],
            )

        return entry

    def list_feedback(
        self,
        status: str | None = None,
        min_score: int | None = None,
        max_score: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """查询反馈记录，支持按状态和分数过滤。"""
        if not self._file.exists():
            return []

        results: list[dict] = []
        with self._lock:
            with open(self._file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if status and entry.get("status") != status:
                        continue
                    if min_score is not None and entry.get("score", 0) < min_score:
                        continue
                    if max_score is not None and entry.get("score", 5) > max_score:
                        continue
                    results.append(entry)

        # 按时间倒序
        results.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return results[:limit]

    def update_status(self, feedback_id: str, status: str) -> bool:
        """更新反馈状态（pending → resolved 等）。"""
        if not self._file.exists():
            return False

        updated = False
        lines: list[str] = []
        with self._lock:
            with open(self._file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("id") == feedback_id:
                            entry["status"] = status
                            updated = True
                        lines.append(json.dumps(entry, ensure_ascii=False))
                    except json.JSONDecodeError:
                        continue
            if updated:
                # 原子写：先写临时文件再 os.replace，避免进程中断产生半截 JSONL
                tmp = self._file.with_suffix(".jsonl.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    for line in lines:
                        f.write(line + "\n")
                os.replace(tmp, self._file)
        return updated

    def stats(self) -> dict:
        """反馈统计。"""
        all_feedback = self.list_feedback(limit=10000)
        if not all_feedback:
            return {"total": 0, "avg_score": 0, "low_score_count": 0}

        scores = [f.get("score", 3) for f in all_feedback]
        low_score = sum(1 for s in scores if s <= LOW_SCORE_THRESHOLD)
        return {
            "total": len(all_feedback),
            "avg_score": round(sum(scores) / len(scores), 2),
            "low_score_count": low_score,
            "low_score_rate": round(low_score / len(scores), 4),
            "pending_count": sum(
                1 for f in all_feedback if f.get("status") == "pending"
            ),
        }


_store: FeedbackStore | None = None


def get_feedback_store() -> FeedbackStore:
    """返回全局反馈存储实例。"""
    global _store
    if _store is None:
        _store = FeedbackStore()
    return _store
