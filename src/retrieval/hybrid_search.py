"""混合检索：Dense Vector + Sparse BM25 + RRF Fusion。

两段式检索架构：
1. 粗排召回：向量语义检索（dense）+ 全量语料 BM25 关键词稀疏检索（sparse）
2. RRF（Reciprocal Rank Fusion）融合两组候选

2.0 升级：BM25 由「dense 子集内二次检索」升级为**全量语料分片索引**——
按知识库缓存 BM25 索引（键含语料指纹），入库/删除后自动失效重建，
关键词精确召回不再受限于向量粗排候选集。
"""
from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache
from typing import Any

from langchain_core.documents import Document

RRF_K = 60.0

# 人工标注分片（metadata.highlight=True）的检索加权系数（v3.0 入库审核流）
# 最终分 = RRF 分 × (1 + HIGHLIGHT_WEIGHT)，初值 0.2，上线后结合 Eval 调参
HIGHLIGHT_WEIGHT = 0.2

# 语料版本号：每次入库/删除该 kb 时 +1，用于让全量 BM25 缓存失效
_corpus_epoch: dict[str, int] = defaultdict(int)


class BM25Searcher:
    """轻量 BM25 稀疏检索（不依赖 rank_bm25 库，便于演示模式离线运行）。"""

    def __init__(self, docs: list[Document], k1: float = 1.5, b: float = 0.75) -> None:
        self._docs = docs
        self._k1 = k1
        self._b = b
        self._avgdl = 0.0
        self._idf: dict[str, float] = {}
        self._terms: list[dict[str, int]] = []
        self._build()

    def _tokenize(self, text: str) -> list[str]:
        """中文分词：优先 jieba，回退到 2-gram 字符切分（避免整句被当单个词）。"""
        try:
            import jieba  # type: ignore

            words = [w for w in jieba.cut(text) if w.strip()]
            if words:
                return words
        except ImportError:
            pass
        # 回退：中文按 2-gram，英文/数字按单词
        tokens: list[str] = []
        for seg in re.findall(r"[\u4e00-\u9fa5]+|[a-z0-9]+", text.lower()):
            if not seg:
                continue
            if seg[0] in "abcdefghijklmnopqrstuvwxyz0123456789":
                tokens.append(seg)
            else:
                # 中文 2-gram
                tokens.extend(
                    seg[i : i + 2] for i in range(len(seg) - 1)
                ) if len(seg) >= 2 else tokens.append(seg)
        return tokens

    def _build(self) -> None:
        df: dict[str, int] = defaultdict(int)
        n = len(self._docs)
        total_len = 0
        for d in self._docs:
            text = getattr(d, "page_content", str(d))
            toks = self._tokenize(text)
            total_len += len(toks)
            freq: dict[str, int] = defaultdict(int)
            for t in toks:
                freq[t] += 1
            self._terms.append(freq)
            for t in set(toks):
                df[t] += 1
        self._avgdl = total_len / n if n else 0.0
        for t, d in df.items():
            self._idf[t] = ((n - d + 0.5) / (d + 0.5) + 1.0)

    def search(self, query: str, top_k: int = 10) -> list[Document]:
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return self._docs[:top_k]
        scored: list[tuple[float, int]] = []
        for i, freq in enumerate(self._terms):
            dl = sum(freq.values())
            score = 0.0
            for t in q_tokens:
                if t not in freq:
                    continue
                tf = freq[t]
                idf = self._idf.get(t, 0.0)
                denom = tf + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
                score += idf * tf / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda x: x[0], reverse=True)
        result = []
        for score, i in scored[:top_k]:
            d = self._docs[i]
            d.metadata["sparse_score"] = round(score, 4)
            result.append(d)
        return result


def invalidate_corpus(kb_id: str) -> None:
    """使指定知识库的全量 BM25 索引失效（入库/删除后调用）。"""
    _corpus_epoch[kb_id] += 1


def _vectorstore_for_kb(kb_id: str) -> Any:
    """按知识库解析向量库实例（collection 隔离）。"""
    from src.core import get_container

    return get_container().get_vectorstore(kb_id)


def _dense_search(query: str, top_k: int = 20, kb_id: str = "default") -> list[Document]:
    vs = _vectorstore_for_kb(kb_id)
    docs = vs.similarity_search(query, k=top_k)
    for d in docs:
        d.metadata.setdefault("dense_score", 1.0)
    return docs


def _fetch_all_chunks(vs: Any) -> list[Document]:
    """拉取该知识库全部分片（供全量 BM25 建索引）。

    内存向量库等无 _collection 的实现返回空列表（此时稀疏检索退化为空，
    混合检索自动降级为纯向量检索，保证可用性）。
    """
    try:
        collection = vs._collection
        data = collection.get(include=["documents", "metadatas"])
        ids = data.get("ids", []) or []
        docs_text = data.get("documents", []) or []
        metas = data.get("metadatas", []) or []
        result: list[Document] = []
        for i, text in enumerate(docs_text):
            meta = dict(metas[i] or {})
            meta["chunk_id"] = ids[i]
            result.append(Document(page_content=text or "", metadata=meta))
        return result
    except Exception:  # noqa: BLE001
        return []


@lru_cache(maxsize=64)
def _get_full_corpus_bm25(kb_id: str, epoch: int) -> BM25Searcher:
    """构建/复用该知识库的全量语料 BM25 索引（epoch 变化即重建）。"""
    vs = _vectorstore_for_kb(kb_id)
    chunks = _fetch_all_chunks(vs)
    return BM25Searcher(chunks)


def _doc_key(d: Document) -> str:
    """生成文档稳定唯一标识，用于 RRF 去重与融合。

    优先使用 ``chunk_id``（全量 BM25 索引会写入），否则回退到
    ``doc_id + chunk_index``，最后才退回 ``page_content``。
    不能用 page_content 作为唯一 key：多个分片正文可能完全相同，
    会导致 RRF 分数被错误累加、相互覆盖。
    """
    meta = getattr(d, "metadata", {}) or {}
    chunk_id = meta.get("chunk_id")
    if chunk_id:
        return f"chunk:{chunk_id}"
    doc_id = meta.get("doc_id")
    chunk_index = meta.get("chunk_index")
    if doc_id is not None:
        return f"doc:{doc_id}:{chunk_index}"
    return f"text:{getattr(d, 'page_content', '')}"


def _rrf_fusion(
    query: str, dense_docs: list[Document], sparse_docs: list[Document], top_k: int = 5
) -> list[Document]:
    """Reciprocal Rank Fusion 融合（人工标注分片加权）。"""
    rank_scores: dict[str, float] = defaultdict(float)
    doc_map: dict[str, Document] = {}

    def push(docs: list[Document]) -> None:
        for rank, d in enumerate(docs):
            key = _doc_key(d)
            rank_scores[key] += 1.0 / (RRF_K + rank + 1)
            doc_map[key] = d

    push(dense_docs)
    push(sparse_docs)

    # v3.0：人工标注（highlight）分片在 RRF 分数基础上加权后排序
    final_scores = {
        key: rank
        * (1.0 + HIGHLIGHT_WEIGHT)
        if (getattr(doc_map[key], "metadata", {}) or {}).get("highlight")
        else rank
        for key, rank in rank_scores.items()
    }
    ordered = sorted(
        doc_map.items(), key=lambda kv: final_scores[kv[0]], reverse=True
    )
    result = [d for _, d in ordered[:top_k]]
    for d in result:
        d.metadata["score"] = round(final_scores[_doc_key(d)], 4)
        if (getattr(d, "metadata", {}) or {}).get("highlight"):
            d.metadata["highlighted"] = True
    return result


def hybrid_search(
    query: str,
    top_k: int = 5,
    dense_k: int = 20,
    sparse_k: int = 20,
    kb_id: str = "default",
) -> list[Document]:
    """执行混合检索并做 RRF 融合（可按知识库隔离）。

    稀疏检索基于该知识库**全量语料**的 BM25 索引（缓存，入库/删除后自动重建），
    关键词精确召回不再局限于向量粗排候选。
    """
    dense_docs = _dense_search(query, top_k=dense_k, kb_id=kb_id)
    epoch = _corpus_epoch.get(kb_id, 0)
    bm25 = _get_full_corpus_bm25(kb_id, epoch)
    sparse_docs = bm25.search(query, top_k=sparse_k)
    return _rrf_fusion(query, dense_docs, sparse_docs, top_k=top_k)


def dense_search(query: str, top_k: int = 5, kb_id: str = "default") -> list[Document]:
    """纯向量检索（可按知识库隔离）。"""
    return _dense_search(query, top_k=top_k, kb_id=kb_id)
