"""测试用轻量伪实现（不依赖真实 ChromaDB，保证测试离线可跑）。"""
from langchain_core.documents import Document


class FakeCollection:
    """模拟 Chroma Collection：支持 get/delete/count，按元数据 where 过滤。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[dict, str]] = {}

    def get(self, where: dict | None = None, include: list | None = None,
            limit: int | None = None) -> dict:
        ids, metas, docs = [], [], []
        for cid, (meta, text) in self._rows.items():
            if where and not all(meta.get(k) == v for k, v in where.items()):
                continue
            ids.append(cid)
            metas.append(dict(meta))
            docs.append(text)
        if limit is not None:
            ids, metas, docs = ids[:limit], metas[:limit], docs[:limit]
        return {"ids": ids, "metadatas": metas, "documents": docs}

    def delete(self, ids: list[str]) -> None:
        for cid in ids:
            self._rows.pop(cid, None)

    def count(self) -> int:
        return len(self._rows)


class FakeVS:
    """最小向量库替身：支持 _collection + add_documents + similarity_search。

    ``_dense_selector`` 可覆盖 dense 召回行为（模拟 dense 漏召回场景）。
    """

    def __init__(self, docs: list[Document] | None = None) -> None:
        self._collection = FakeCollection()
        self._docs: list[Document] = []
        self._dense_selector = None
        if docs:
            self.add_documents(docs, ids=[f"id-{i}" for i in range(len(docs))])

    def add_documents(self, documents: list[Document], ids: list[str] | None = None) -> None:
        if ids is None:
            start = len(self._collection._rows)
            ids = [f"id-{start + i}" for i in range(len(documents))]
        for doc, cid in zip(documents, ids):
            self._collection._rows[cid] = (dict(doc.metadata), doc.page_content)
            self._docs.append(doc)

    def similarity_search(self, query: str, k: int = 4, **kwargs) -> list[Document]:
        if self._dense_selector is not None:
            return self._dense_selector(self, query, k)
        return self._docs[:k]
