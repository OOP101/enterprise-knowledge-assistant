"""知识库管理器：多知识库 CRUD 与定向检索/入库。"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from config.settings import settings

KB_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "knowledge_bases.json"

# kb_id 直接拼进 Chroma collection 名，必须限制字符集防畸形 ID
_KB_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _now_iso() -> str:
    """本地时间，秒级 ISO 8601（与 users.py / embedder.py 风格一致）。"""
    return _dt.datetime.now().isoformat(timespec="seconds")


class KnowledgeBaseManager:
    """管理知识库清单（基于 JSON 文件，线程安全）。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or KB_FILE
        self._lock = threading.Lock()
        self._kbs: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                self._kbs = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self._kbs = {}

    def _save(self) -> None:
        # 原子写：先写临时文件再 os.replace，避免进程中断产生半截 JSON
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._kbs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    def ensure_default(self) -> None:
        """保证 default 库存在（默认库对所有部门开放）。"""
        if "default" not in self._kbs:
            self._kbs["default"] = {
                "id": "default",
                "name": "默认知识库",
                "description": "系统默认知识库",
                "collection": settings.chroma_collection,
                "departments": [],  # 空 = 所有部门可见
                "created_at": _now_iso(),
            }
            self._save()

    def create(
        self,
        kb_id: str,
        name: str,
        description: str = "",
        departments: list[str] | None = None,
    ) -> dict:
        kb_id = kb_id.strip()
        if not kb_id:
            raise ValueError("知识库 ID 不能为空")
        if not _KB_ID_PATTERN.match(kb_id):
            raise ValueError(
                "知识库 ID 仅允许字母、数字、下划线、连字符，长度 1-64"
            )
        with self._lock:
            if kb_id in self._kbs:
                raise ValueError(f"知识库已存在：{kb_id}")
            self._kbs[kb_id] = {
                "id": kb_id,
                "name": name.strip() or kb_id,
                "description": description,
                "collection": f"{settings.chroma_collection}_{kb_id}",
                "departments": departments or [],  # 空 = 所有部门可见
                "created_at": _now_iso(),
            }
            self._save()
            return dict(self._kbs[kb_id])

    def set_departments(self, kb_id: str, departments: list[str]) -> bool:
        """设置知识库可访问的部门列表（空 = 所有部门可见）。"""
        with self._lock:
            if kb_id not in self._kbs:
                return False
            self._kbs[kb_id]["departments"] = list(departments)
            self._save()
            return True

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._kbs.values()]

    def get(self, kb_id: str) -> dict | None:
        with self._lock:
            return dict(self._kbs[kb_id]) if kb_id in self._kbs else None

    def delete(self, kb_id: str) -> bool:
        with self._lock:
            if kb_id == "default" or kb_id not in self._kbs:
                return False
            del self._kbs[kb_id]
            self._save()
            return True


_manager: KnowledgeBaseManager | None = None


def get_kb_manager() -> KnowledgeBaseManager:
    global _manager
    if _manager is None:
        _manager = KnowledgeBaseManager()
        _manager.ensure_default()
    return _manager
