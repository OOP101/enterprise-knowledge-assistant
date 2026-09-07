"""端口与抽象（Ports & Adapters）。

定义各能力域的抽象接口，使具体实现可插拔、可测试、可替换。
2.0 的核心是"依赖接口，而非依赖实现"。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from langchain_core.documents import Document


class BaseReranker(ABC):
    """重排序器端口。"""

    @abstractmethod
    def rerank(self, query: str, docs: list[Document], top_k: int = 5) -> list[Document]:
        """对粗排召回的候选文档做精排，返回前 top_k。"""

    @abstractmethod
    def name(self) -> str:
        """重排器标识（用于日志与配置展示）。"""


class BaseRetrieverPort(ABC):
    """检索器端口（统一混合/纯向量检索）。"""

    @abstractmethod
    def retrieve(self, query: str, top_k: int = 5) -> list[Document]:
        """检索并返回精排后的 top_k 文档。"""

    @abstractmethod
    def search_with_context(self, query: str, top_k: int = 5) -> tuple[str, list[Document]]:
        """检索并拼接上下文文本。返回 (context_text, docs)。"""


class SessionMemoryPort(ABC):
    """短期会话记忆端口。"""

    @abstractmethod
    def get_history(self, session_id: str) -> list[Any]:
        """取会话历史消息列表。"""

    @abstractmethod
    def add_turn(self, session_id: str, question: str, answer: str) -> None:
        """记录一轮问答。"""

    @abstractmethod
    def clear(self, session_id: str) -> None:
        """清空会话。"""


class VectorMemoryPort(ABC):
    """长期向量记忆端口。"""

    @abstractmethod
    def remember(self, session_id: str, user: str, summary: str) -> str:
        """持久化一条长期记忆，返回记忆 ID。"""

    @abstractmethod
    def recall(self, query: str, top_k: int = 3) -> list[str]:
        """跨会话检索相关长期记忆。"""


class LLMProvider(ABC):
    """LLM 提供方端口（便于 fake 注入测试）。"""

    @abstractmethod
    def invoke_text(self, prompt: str, **kwargs: Any) -> str:
        """以纯文本方式调用 LLM 并返回字符串。"""

    @abstractmethod
    def stream_text(self, prompt: str, **kwargs: Any):
        """流式调用 LLM，逐块产出文本。"""
