"""用户对话历史管理（会话列表 + 消息记录 + 追问记录）。

存储结构：
    data/chat_history/
    └── {username}/
        ├── sessions.json          # 会话列表
        └── {session_id}/
            └── messages.jsonl     # 消息记录（逐行 JSON，追加写入）
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from config.settings import settings

HISTORY_BASE = Path(__file__).resolve().parent.parent.parent / "data" / "chat_history"


class ChatHistoryStore:
    """基于 JSON + JSONL 文件的用户对话历史存储。"""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base = base_dir or HISTORY_BASE
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _user_dir(self, username: str) -> Path:
        return self._base / username

    def _lock_for(self, key: str) -> threading.Lock:
        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def create_session(self, username: str, title: str = "", session_id: str | None = None) -> dict:
        session_id = session_id or uuid.uuid4().hex[:12]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        session = {
            "id": session_id,
            "title": title or "新对话",
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }
        user_dir = self._user_dir(username)
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / session_id).mkdir(parents=True, exist_ok=True)

        sessions = self._load_sessions(username)
        # 若已存在同 id 会话则跳过重复登记（幂等，避免问答接口并发创建时重复插入）
        if not any(s.get("id") == session_id for s in sessions):
            sessions.insert(0, session)
            # sessions.json 的读改写统一走用户级锁，与 rename/delete/_touch_session 互斥
            with self._lock_for(f"{username}:sessions"):
                self._save_sessions(username, sessions)
        return session

    def list_sessions(self, username: str, limit: int = 50) -> list[dict]:
        sessions = self._load_sessions(username)
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return sessions[:limit]

    def get_session(self, username: str, session_id: str) -> dict | None:
        sessions = self._load_sessions(username)
        for s in sessions:
            if s["id"] == session_id:
                return s
        return None

    def rename_session(self, username: str, session_id: str, title: str) -> dict | None:
        lock = self._lock_for(f"{username}:sessions")
        with lock:
            sessions = self._load_sessions(username)
            for s in sessions:
                if s["id"] == session_id:
                    s["title"] = title or s["title"]
                    s["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                    self._save_sessions(username, sessions)
                    return s
        return None

    def delete_session(self, username: str, session_id: str) -> bool:
        import shutil

        lock = self._lock_for(f"{username}:sessions")
        with lock:
            sessions = self._load_sessions(username)
            sessions = [s for s in sessions if s["id"] != session_id]
            self._save_sessions(username, sessions)
            session_dir = self._user_dir(username) / session_id
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
            return True

    def add_message(
        self,
        username: str,
        session_id: str,
        role: str,
        content: str,
        sources: list[dict] | None = None,
        score: int | None = None,
        is_followup: bool = False,
    ) -> dict:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        msg = {
            "id": uuid.uuid4().hex[:8],
            "role": role,
            "content": content,
            "sources": sources or [],
            "score": score,
            "is_followup": is_followup,
            "created_at": now,
        }

        lock = self._lock_for(f"{username}:{session_id}")
        with lock:
            session_dir = self._user_dir(username) / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            msg_file = session_dir / "messages.jsonl"
            with open(msg_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        # _touch_session 会写 sessions.json，统一用用户级锁，
        # 避免与 rename/delete（f"{username}:sessions" 锁）互相覆盖丢更新
        with self._lock_for(f"{username}:sessions"):
            self._touch_session(username, session_id, content if role == "user" else None)
        return msg

    def get_messages(
        self, username: str, session_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        msg_file = self._user_dir(username) / session_id / "messages.jsonl"
        if not msg_file.exists():
            return []
        messages: list[dict] = []
        with open(msg_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return messages[offset : offset + limit]

    def get_followups(self, username: str, session_id: str) -> list[dict]:
        messages = self.get_messages(username, session_id, limit=9999)
        return [m for m in messages if m.get("is_followup")]

    def search_messages(self, username: str, keyword: str, limit: int = 20) -> list[dict]:
        sessions = self._load_sessions(username)
        results: list[dict] = []
        for s in sessions:
            messages = self.get_messages(username, s["id"], limit=9999)
            for m in messages:
                if keyword.lower() in m.get("content", "").lower():
                    results.append({**m, "session_id": s["id"], "session_title": s["title"]})
                    if len(results) >= limit:
                        return results
        return results

    def _touch_session(self, username: str, session_id: str, user_msg: str | None) -> None:
        sessions = self._load_sessions(username)
        for s in sessions:
            if s["id"] == session_id:
                s["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                s["message_count"] = s.get("message_count", 0) + 1
                if user_msg and (s.get("title") == "新对话" or not s.get("title")):
                    s["title"] = user_msg[:30]
                break
        self._save_sessions(username, sessions)

    def _load_sessions(self, username: str) -> list[dict]:
        f = self._user_dir(username) / "sessions.json"
        if not f.exists():
            return []
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []

    def _save_sessions(self, username: str, sessions: list[dict]) -> None:
        # 原子写：先写临时文件再 os.replace，避免进程中断产生半截 JSON
        user_dir = self._user_dir(username)
        user_dir.mkdir(parents=True, exist_ok=True)
        target = user_dir / "sessions.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)


_store: ChatHistoryStore | None = None


def get_chat_history_store() -> ChatHistoryStore:
    global _store
    if _store is None:
        _store = ChatHistoryStore()
    return _store
