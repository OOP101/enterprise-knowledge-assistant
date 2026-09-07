"""Token 用量监控。

跟踪每次 LLM 调用的 Token 消耗，提供统计与告警。
项目文档规划：「全局 Token 消耗监控与告警」
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_WARN_THRESHOLD = 8000

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "usage"
USAGE_FILE = DATA_DIR / "token_usage.jsonl"


class TokenTracker:
    """线程安全的 Token 用量跟踪器（记录持久化到 JSONL）。"""

    def __init__(self, file_path: Path | None = None) -> None:
        self._lock = Lock()
        self._file = file_path or USAGE_FILE
        self._total_input = 0
        self._total_output = 0
        self._call_count = 0
        self._by_model: dict[str, dict] = defaultdict(
            lambda: {"input": 0, "output": 0, "calls": 0}
        )
        self._recent_calls: list[dict] = []
        self._max_recent = 100
        self._load()

    def _load(self) -> None:
        """从 JSONL 加载历史记录。"""
        if not self._file.exists():
            return
        try:
            with self._lock, open(self._file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._total_input += entry.get("input", 0)
                    self._total_output += entry.get("output", 0)
                    self._call_count += 1
                    model = entry.get("model", "unknown")
                    self._by_model[model]["input"] += entry.get("input", 0)
                    self._by_model[model]["output"] += entry.get("output", 0)
                    self._by_model[model]["calls"] += 1
                    self._recent_calls.append(entry)
                    if len(self._recent_calls) > self._max_recent:
                        self._recent_calls.pop(0)
        except Exception as e:
            logger.warning("加载 Token 用量记录失败：%s", e)

    def _append(self, entry: dict) -> None:
        """在锁保护下追加一条记录到文件。"""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("持久化 Token 用量记录失败：%s", e)

    def record(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str = "unknown",
        endpoint: str = "",
    ) -> None:
        """记录一次 LLM 调用的 Token 消耗。"""
        entry = {
            "timestamp": time.time(),
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
            "model": model,
            "endpoint": endpoint,
        }
        with self._lock:
            self._total_input += input_tokens
            self._total_output += output_tokens
            self._call_count += 1
            self._by_model[model]["input"] += input_tokens
            self._by_model[model]["output"] += output_tokens
            self._by_model[model]["calls"] += 1

            self._recent_calls.append(entry)
            if len(self._recent_calls) > self._max_recent:
                self._recent_calls.pop(0)

            total = input_tokens + output_tokens
            if total > TOKEN_WARN_THRESHOLD:
                logger.warning(
                    "Token 用量告警：单次调用 %d tokens (input=%d, output=%d, model=%s)",
                    total, input_tokens, output_tokens, model,
                )

            self._append(entry)

    def stats(self) -> dict:
        """返回汇总统计。"""
        with self._lock:
            return {
                "total_input": self._total_input,
                "total_output": self._total_output,
                "total": self._total_input + self._total_output,
                "call_count": self._call_count,
                "avg_per_call": round(
                    (self._total_input + self._total_output) / self._call_count, 1
                ) if self._call_count else 0,
                "by_model": dict(self._by_model),
                "recent_calls": list(self._recent_calls[-20:]),
            }

    def reset(self) -> None:
        """重置统计并清空持久化文件。"""
        with self._lock:
            self._total_input = 0
            self._total_output = 0
            self._call_count = 0
            self._by_model.clear()
            self._recent_calls.clear()
            try:
                if self._file.exists():
                    self._file.unlink()
            except Exception as e:
                logger.warning("清空 Token 用量记录失败：%s", e)


_tracker = TokenTracker()


def get_token_tracker() -> TokenTracker:
    return _tracker


def estimate_tokens(text: str) -> int:
    """粗略估算 Token 数。"""
    chinese_chars = len(re.findall(r"[\u4e00-\u9fa5]", text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars * 1.5 + other_chars * 0.25)
