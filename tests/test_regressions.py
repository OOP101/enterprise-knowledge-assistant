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


# ---- 8. 批量入库：不支持的格式必须显式上报，不得静默丢弃 ----
def test_ingest_reports_unsupported_formats(tmp_path, monkeypatch):
    from config.settings import get_settings
    from serve.routers.knowledge import ingest_directory
    from src.ingestion.loader import SUPPORTED_EXTENSIONS

    # 从常量推导"当前不支持的格式"，避免每新增一种格式就要改这条用例
    junk_exts = [e for e in (".wps", ".xls", ".csv", ".rtf") if e not in SUPPORTED_EXTENSIONS]
    assert len(junk_exts) >= 2, "需至少两种未支持格式来覆盖该场景"

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    names = [f"junk{i}{ext}" for i, ext in enumerate(junk_exts[:2])]
    for n in names:
        (docs_dir / n).write_bytes(b"not a supported document")
    # 支持格式但内容无效 → 解析失败，也应上报而非静默跳过
    (docs_dir / "c.pdf").write_bytes(b"not a real pdf")

    monkeypatch.setattr(get_settings(), "docs_dir", str(docs_dir))

    r = ingest_directory(kb_id="default", _=None)

    assert sorted(r["unsupported"]) == sorted(names)
    assert r["skipped_empty"] == ["c.pdf"]
    assert r["ingested"] == 0
    assert r["files"] == []
    assert "格式不支持" in r["message"]
    assert "解析后无内容" in r["message"]


def test_ingest_clean_dir_reports_empty_buckets(tmp_path, monkeypatch):
    from config.settings import get_settings
    from serve.routers.knowledge import ingest_directory

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    monkeypatch.setattr(get_settings(), "docs_dir", str(docs_dir))

    r = ingest_directory(kb_id="default", _=None)

    assert r["unsupported"] == []
    assert r["skipped_empty"] == []
    assert "⚠" not in r["message"]


# ---- 9. .doc（Word 97-2003 二进制）解析：piece table + 控制符清洗 ----
def test_doc_registered_as_supported():
    from src.ingestion.loader import _LOADERS, SUPPORTED_EXTENSIONS

    assert ".doc" in SUPPORTED_EXTENSIONS
    assert ".doc" in _LOADERS


def test_normalize_doc_text_strips_control_chars():
    from src.ingestion.loader import _normalize_doc_text

    raw = "标题\r第一章\x0b第一节\x0c\x00正文\xa0结束\r\r\r"
    assert _normalize_doc_text(raw) == "标题\n第一章\n第一节\n正文 结束"


def test_doc_text_runs_non_complex_single_block():
    from src.ingestion.loader import _doc_text_runs

    fib = bytearray(64)  # flags 全 0 → fComplex=0
    fib[0x18:0x1C] = (100).to_bytes(4, "little")  # fcMin
    fib[0x1C:0x20] = (300).to_bytes(4, "little")  # fcMac

    assert _doc_text_runs(bytes(fib), b"") == [(100, 200, True)]


def test_load_doc_on_non_ole_returns_empty(tmp_path):
    """非 OLE 文件（如被改名的文本）应安全返回空，而不是抛异常中断整批入库。"""
    from src.ingestion.loader import _load_doc

    f = tmp_path / "fake.doc"
    f.write_bytes(b"this is not an OLE compound document at all")

    assert _load_doc(f) == []


# ---- 10. .xlsx（OOXML）：标准库解析，共享串 / 内联串 / 表名 ----
def _make_xlsx(path) -> None:
    """合成一个最小 .xlsx（本质是 zip）用于离线测试。"""
    import zipfile

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    files = {
        "xl/workbook.xml": (
            f'<workbook xmlns="{ns}" xmlns:r="{rel_ns}">'
            '<sheets><sheet name="职责表" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{pkg_ns}">'
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/sharedStrings.xml": (
            f'<sst xmlns="{ns}"><si><t>职责</t></si><si><t>说明</t></si></sst>'
        ),
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{ns}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2"><v>42</v></c>'
            '<c r="B2" t="inlineStr"><is><t>内联</t></is></c></row>'
            "</sheetData></worksheet>"
        ),
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)


def test_xlsx_extracts_shared_and_inline_strings(tmp_path):
    from src.ingestion.loader import _load_xlsx

    f = tmp_path / "t.xlsx"
    _make_xlsx(f)

    docs = _load_xlsx(f)

    assert len(docs) == 1
    text = docs[0].page_content
    assert "[职责表]" in text  # 表名取自 workbook.xml
    assert "| 职责 | 说明 |" in text  # 共享字符串
    assert "| 42 | 内联 |" in text  # 字面值 + 内联字符串


def test_xlsx_registered_as_supported():
    from src.ingestion.loader import _LOADERS, SUPPORTED_EXTENSIONS

    assert ".xlsx" in SUPPORTED_EXTENSIONS
    assert ".xlsx" in _LOADERS
