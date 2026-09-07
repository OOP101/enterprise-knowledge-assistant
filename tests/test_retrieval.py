"""检索模块测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.documents import Document

from src.models.reranker import DemoReranker
from src.retrieval.hybrid_search import BM25Searcher, hybrid_search
from tests.fakes import FakeVS


def test_bm25_finds_relevant():
    docs = [
        Document(page_content="请假需要填写申请单并提交主管审批"),
        Document(page_content="加班时长可调休或按工资计算"),
        Document(page_content="报销流程需提供发票"),
    ]
    searcher = BM25Searcher(docs)
    result = searcher.search("请假流程", top_k=3)
    assert result, "BM25 应返回结果"
    # 检索结果中应至少包含与"请假"或"流程"相关的文档
    assert any(("请假" in d.page_content or "流程" in d.page_content) for d in result)


def test_rrf_fusion_returns_topk(monkeypatch):
    import src.retrieval.hybrid_search as hs

    vs = FakeVS(
        [
            Document(page_content="文档一：关于请假制度的说明"),
            Document(page_content="文档二：关于请假审批的细节"),
            Document(page_content="文档三：关于加班调休"),
        ]
    )
    monkeypatch.setattr(hs, "_vectorstore_for_kb", lambda kb_id: vs)
    hs.invalidate_corpus("kb_rrf")
    fused = hybrid_search("请假", top_k=2, kb_id="kb_rrf")
    assert isinstance(fused, list)
    assert len(fused) <= 2


def test_rerank_orders_by_relevance():
    docs = [
        Document(page_content="完全无关的内容，讲述天气与足球"),
        Document(page_content="请假申请流程详细说明与审批要求"),
        Document(page_content="另一个关于请假的补充条款"),
    ]
    ordered = DemoReranker().rerank("请假流程", docs, top_k=3)
    assert ordered[0].page_content != "完全无关的内容，讲述天气与足球"
