"""短期会话记忆。

以 session_id 为键，维护每轮对话消息，供多轮问答与指代消解使用。
- 可返回 LangChain 的 HumanMessage / AIMessage 列表（兼容 MessagesPlaceholder）
"""
from __future__ import annotations

import threading
from collections import OrderedDict

from langchain_core.messages import AIMessage, HumanMessage

from config.settings import settings


class ConversationMemory:
    """进程内会话记忆（可按需替换为 Redis 等分布式实现）。"""

    def __init__(self, max_turns: int | None = None) -> None:
        self._store: dict[str, list] = {}
        self._lock = threading.Lock()
        self._max_turns = max_turns or settings.memory_k

    def add_turn(self, session_id: str, question: str, answer: str) -> None:
        with self._lock:
            history = self._store.setdefault(session_id, [])
            history.append(HumanMessage(content=question))
            history.append(AIMessage(content=answer))
            # 只保留最近 max_turns 轮
            keep = self._max_turns * 2
            if len(history) > keep:
                self._store[session_id] = history[-keep:]

    def get_history(self, session_id: str) -> list:
        return list(self._store.get(session_id, []))

    def get_lc_history(self, session_id: str) -> list:
        """返回 LangChain message 列表。"""
        return self.get_history(session_id)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

    def stats(self) -> dict:
        return {"sessions": len(self._store)}


_global_memory = ConversationMemory()


def get_conversation_memory() -> ConversationMemory:
    return _global_memory
