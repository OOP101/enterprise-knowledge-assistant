"""可观测性基础设施：结构化日志 + 请求追踪（2.0）。

- structlog 优先，回退标准 logging。
- 提供 TraceContext（request_id）贯穿一次问答。
"""
from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

try:
    import structlog  # type: ignore

    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover
    structlog = None  # type: ignore
    _HAS_STRUCTLOG = False

# 请求上下文变量（跨协程/线程传递 request_id）
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def set_trace_id(tid: str) -> None:
    _trace_id.set(tid)


def get_trace_id() -> str:
    return _trace_id.get()


def get_logger(name: str = "app") -> Any:
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)


@lru_cache
def setup_logging(level: str = "INFO") -> None:
    """初始化根日志与 structlog 渲染。应用启动时调用一次。"""
    if _HAS_STRUCTLOG:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level, logging.INFO)
            ),
            logger_factory=structlog.PrintLoggerFactory(),
        )
    else:
        logging.basicConfig(level=getattr(logging, level, logging.INFO))


class Timer:
    """简单耗时计时器。"""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000, 2)
