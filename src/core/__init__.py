"""核心抽象与依赖注入（2.0）。"""

from src.core.container import Container, get_container
from src.core.interfaces import (
    BaseReranker,
    BaseRetrieverPort,
    SessionMemoryPort,
    VectorMemoryPort,
)

__all__ = [
    "Container",
    "get_container",
    "BaseReranker",
    "BaseRetrieverPort",
    "SessionMemoryPort",
    "VectorMemoryPort",
]
