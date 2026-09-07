"""回归测试：覆盖本轮优化修复的关键缺陷（离线可跑，不依赖真实 ChromaDB / API）。"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.documents import Document

from tests.fakes import FakeVS


# ---- 1. 检索器：retrieve() 方法（修复 Eval 崩溃）+ kb_id 隔离 ----
def test_hybrid_retriever_retrieve_and_kb(monkeypatch):
    import src.retrieval.retriever as rt
    from src.retrieval.retriever import HybridRetriever

    captured = {}

    def fake_hybrid(query, top_k=15, kb_id="default"):
        captured["top_k"] = top_k
        captured["kb_id"] = kb_id
        return [Document(page_content=f"doc-{i}") for i in range(top_k)]

    monkeypatch.setattr(rt, "hybrid_search", fake_hybrid)
    r = HybridRetriever(top_k=3, kb_id="hr", reranker=None)

    docs = r.retrieve("问题", top_k=4)
    assert len(docs) == 4
    assert captured["top_k"] == 12  # 粗排按 top_k*3
    assert captured["kb_id"] == "hr"

    # invoke 走 _get_relevant_documents → 使用实例 top_k
    docs2 = r.invoke("问题")
    assert len(docs2) == 3


def test_container_retriever_kb_isolation():
    from src.core import get_container

    c = get_container()
    r_default = c.get_retriever(kb_id="default", top_k=5)
    r_hr = c.get_retriever(kb_id="hr", top_k=5)
    assert r_default.kb_id == "default"
    assert r_hr.kb_id == "hr"
    assert r_default is not r_hr
    # use_hybrid 不同 → 独立实例
    r_pure = c.get_retriever(kb_id="default", top_k=5, use_hybrid=False)
    assert r_pure is not r_default


# ---- 2. Embedding：演示向量跨进程稳定（修复内置 hash 随机化）----
def test_demo_embeddings_stable_across_instances():
    from src.models.embedding import DemoEmbeddings

    a, b = DemoEmbeddings(), DemoEmbeddings()
    va = a.embed_query("请假流程如何办理")
    vb = b.embed_query("请假流程如何办理")
    assert va == vb
    assert a._stable_index("请假", 512) == b._stable_index("请假", 512)
    assert len(va) == DemoEmbeddings.dimension


# ---- 3. LLM 配置：不回传完整 Key；空 Key 不误清空 ----
def test_llm_config_hides_api_key(monkeypatch):
    import src.models.llm as llm_mod

    class FakeManager:
        PROVIDERS = {}

        def __init__(self):
            self._config = {"base_url": "x", "api_key": "sk-1234567890abcdef", "model": "m", "provider": "p"}

        def get_config(self):
            return dict(self._config)

        @property
        def ready(self):
            return True

    monkeypatch.setattr(llm_mod, "_manager", FakeManager())
    cfg = llm_mod.get_llm_config()
    assert cfg["api_key"] == ""
    # key = "sk-1234567890abcdef" → key[:6]="sk-123" + "****" + key[-4:]="cdef"
    assert cfg["api_key_masked"] == "sk-123****cdef"


def test_configure_empty_key_keeps_existing(tmp_path, monkeypatch):
    from src.models.llm import LLMManager

    m = LLMManager()
    m.CONFIG_FILE = tmp_path / "model_config.json"  # 避免写真实配置文件
    m._config["api_key"] = "sk-keep"
    m.configure(api_key="")
    assert m._config["api_key"] == "sk-keep"
    m.configure(api_key="   ")
    assert m._config["api_key"] == "sk-keep"
    m.configure(api_key="sk-new")
    assert m._config["api_key"] == "sk-new"


# ---- 4. 用户 created_at 为真实时间戳 ----
def test_user_created_at_is_timestamp(tmp_path):
    from src.auth.users import UserStore

    store = UserStore(path=tmp_path / "users.json")
    user = store.register("u1", "pwd12345", department="hr")
    dt = datetime.datetime.fromisoformat(user["created_at"])
    assert dt.year >= 2020


# ---- 5. 入库：分片级增量更新 + 内容 hash 去重 ----
def test_ingest_incremental_updates_chunks(monkeypatch):
    from src.ingestion import embedder as emb

    vs = FakeVS()
    monkeypatch.setattr(emb, "get_vectorstore", lambda **kwargs: vs)

    docs = [Document(page_content=f"第{i}段内容", metadata={"source": "t.md"}) for i in range(3)]
    r1 = emb.ingest_documents(docs, doc_id="d1")
    assert r1["ingested"] == 3
    assert vs._collection.count() == 3

    # 仅中间一段变化 → 只新增 1 个分片，总数不变
    docs2 = [Document(page_content=f"第{i}段内容", metadata={"source": "t.md"}) for i in range(3)]
    docs2[1] = Document(page_content="第1段内容（已修改）", metadata={"source": "t.md"})
    r2 = emb.ingest_documents(docs2, doc_id="d1")
    assert r2["ingested"] == 1
    assert vs._collection.count() == 3
    assert vs._collection._rows["d1-1"][1] == "第1段内容（已修改）"

    # 分片数量减少 → 删除多余旧分片
    docs3 = [Document(page_content="第0段内容", metadata={"source": "t.md"})]
    r3 = emb.ingest_documents(docs3, doc_id="d1")
    assert r3["ingested"] == 0  # 复用
    assert vs._collection.count() == 1


def test_ingest_dedup_by_content_hash(monkeypatch):
    from src.ingestion import embedder as emb

    vs = FakeVS()
    monkeypatch.setattr(emb, "get_vectorstore", lambda **kwargs: vs)

    docs = [Document(page_content="内容", metadata={"source": "t.md"})]
    r1 = emb.ingest_documents(docs, content_hash="abc123")
    assert r1["skipped"] is False
    r2 = emb.ingest_documents(docs, content_hash="abc123")
    assert r2["skipped"] is True


# ---- 6. Eval：runner 兼容 retrieve() 端口（修复此前 AttributeError 崩溃）----
def test_run_eval_with_retriever_port(tmp_path):
    from src.eval.runner import run_eval

    dataset = tmp_path / "eval.json"
    dataset.write_text(
        json.dumps(
            [{"question": "请假流程？", "gold_docs": ["a.md"], "gold_answer": "填写申请单"}]
        ),
        encoding="utf-8",
    )

    class StubRetriever:
        def retrieve(self, query, top_k=5):
            return [Document(page_content="请假需填写申请单", metadata={"source": "a.md"})]

    result = run_eval(StubRetriever(), dataset_path=dataset)
    assert result["total"] == 1
    assert result["hit_rate"] == 1.0


# ---- 7. 全量语料 BM25：关键词精确召回 + 缓存失效 ----
def test_full_corpus_bm25_recalls_keyword(monkeypatch):
    import src.retrieval.hybrid_search as hs

    vs = FakeVS(
        [
            Document(page_content="公司周年庆活动安排"),
            Document(page_content="报销流程需提供发票并提交审批"),
            Document(page_content="员工健康体检通知"),
        ]
    )
    # 模拟 dense 召回漏掉关键词文档（只命中第 0 条）
    vs._dense_selector = lambda self, q, k: [self._docs[0]]
    monkeypatch.setattr(hs, "_vectorstore_for_kb", lambda kb_id: vs)

    hs.invalidate_corpus("kb_fullcorpus")
    result = hs.hybrid_search("报销需要什么材料", top_k=3, kb_id="kb_fullcorpus")
    assert any("报销" in d.page_content for d in result), "全量 BM25 应召回关键词文档"


def test_bm25_corpus_epoch_invalidates(monkeypatch):
    import src.retrieval.hybrid_search as hs

    vs = FakeVS([Document(page_content="旧内容")])
    monkeypatch.setattr(hs, "_vectorstore_for_kb", lambda kb_id: vs)

    e0 = hs._corpus_epoch.get("kb_inv", 0)
    i1 = hs._get_full_corpus_bm25("kb_inv", e0)
    hs.invalidate_corpus("kb_inv")
    assert hs._corpus_epoch["kb_inv"] == e0 + 1
    i2 = hs._get_full_corpus_bm25("kb_inv", e0 + 1)
    assert i1 is not i2, "epoch 变化后应重建 BM25 索引"
