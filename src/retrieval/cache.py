"""热查询缓存（LRU）。

对高频重复问题缓存检索结果与回答，跳过检索与 LLM 调用，降低响应延迟。
项目文档规划：「缓存策略: LRU Cache (最近100条查询)」

缓存维度：(query_normalized, kb_id) -> {answer, sources, timestamp}
缓存失效：文档入库/删除后自动清空对应知识库的缓存。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

# 缓存默认配置
DEFAULT_CACHE_SIZE = 100
DEFAULT_TTL_SECONDS = 3600  # 1 小时后过期

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
CACHE_FILE = DATA_DIR / "query_cache.json"


class QueryCache:
    """线程安全的 LRU 查询缓存（持久化到本地 JSON）。"""

    def __init__(
        self,
        max_size: int = DEFAULT_CACHE_SIZE,
        ttl: int = DEFAULT_TTL_SECONDS,
        cache_file: Path | None = None,
    ) -> None:
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl
        self._lock = Lock()
        self._hits = 0
        self._misses = 0
        self._cache_file = cache_file or CACHE_FILE
        self._load()

    def _load(self) -> None:
        """从文件加载缓存，并清理过期条目。"""
        if not self._cache_file.exists():
            return
        try:
            with open(self._cache_file, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("加载缓存文件失败：%s", e)
            return

        now = time.time()
        hits = data.get("hits", 0)
        misses = data.get("misses", 0)
        entries = data.get("entries", {})
        valid_entries: OrderedDict[str, dict] = OrderedDict()
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            if now - entry.get("timestamp", 0) > self._ttl:
                continue
            valid_entries[key] = entry

        with self._lock:
            self._cache = valid_entries
            self._hits = hits
            self._misses = misses

    def _snapshot(self) -> dict:
        """生成当前缓存快照。调用方必须持有 self._lock。"""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "entries": dict(self._cache),
        }

    def _save(self, data: dict) -> None:
        """保存缓存到文件。data 由调用方在锁内准备好，避免死锁。"""
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("持久化缓存失败：%s", e)

    @staticmethod
    def _make_key(query: str, kb_id: str = "default") -> str:
        """生成缓存键：归一化查询 + kb_id 的 md5。"""
        normalized = query.strip().lower()
        raw = f"{normalized}|{kb_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def get(self, query: str, kb_id: str = "default") -> dict | None:
        """查询缓存。命中返回 {answer, sources}，未命中返回 None。"""
        if not settings.cache_enabled:
            return None
        key = self._make_key(query, kb_id)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                snapshot = self._snapshot()
                self._save(snapshot)
                return None

            # TTL 过期检查
            if time.time() - entry["timestamp"] > self._ttl:
                del self._cache[key]
                self._misses += 1
                snapshot = self._snapshot()
                self._save(snapshot)
                return None

            # LRU: 移到末尾（最近使用）
            self._cache.move_to_end(key)
            self._hits += 1
            logger.debug("缓存命中：%s (kb=%s)", query[:30], kb_id)
            snapshot = self._snapshot()
            result = {
                "answer": entry["answer"],
                "sources": entry["sources"],
            }
        self._save(snapshot)
        return result

    def put(
        self,
        query: str,
        answer: str,
        sources: list,
        kb_id: str = "default",
    ) -> None:
        """写入缓存。"""
        if not settings.cache_enabled:
            return
        key = self._make_key(query, kb_id)
        with self._lock:
            self._cache[key] = {
                "answer": answer,
                "sources": sources,
                "timestamp": time.time(),
            }
            self._cache.move_to_end(key)

            # 超容量淘汰最久未使用的
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
            snapshot = self._snapshot()
        self._save(snapshot)

    def invalidate_kb(self, kb_id: str = "default") -> int:
        """文档入库/删除后清空指定知识库的缓存。返回清除条数。"""
        cleared = 0
        with self._lock:
            # 由于 key 是 hash，无法反查 kb_id，改为清空全部缓存
            # 这是最安全的策略：文档变更后全部失效
            cleared = len(self._cache)
            self._cache.clear()
            snapshot = self._snapshot()
        if cleared:
            logger.info("查询缓存已清空（%d 条，kb=%s 文档变更）", cleared, kb_id)
        self._save(snapshot)
        return cleared

    def clear(self) -> None:
        """清空全部缓存。"""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            snapshot = self._snapshot()
        self._save(snapshot)

    def stats(self) -> dict:
        """返回缓存统计信息。"""
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }


# 全局缓存实例
_cache = QueryCache()


def get_query_cache() -> QueryCache:
    """返回全局查询缓存实例。"""
    return _cache
