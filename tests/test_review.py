"""v3.0 入库审核流测试：打分器 / 审核存储状态机 / 流水线 / 检索加权。

全部离线可跑（打分器纯规则，pipeline 注入 fake ingest）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.documents import Document

from src.review.pipeline import (
    apply_annotations,
    approve_document,
    process_and_ingest,
    reject_document,
)
from src.review.scorer import score_document
from src.review.store import ReviewStore


# ---------- 打分器 ----------

GOOD_DOC = (
    "第一章 总则\n\n"
    "第一条 为规范公司请假管理，特制定本制度。本制度适用于全体正式员工。\n\n"
    "第二条 员工请假需提前一个工作日在 OA 系统提交申请，由直属上级审批。\n\n"
    "第二章 事假管理\n\n"
    "第三条 事假期间工资按日薪的百分之六十发放，当月事假累计不得超过三天。\n\n"
    "第四条 未经审批擅自离岗的，按旷工处理，扣除当日全部工资。\n\n"
) * 4  # 约 600+ 字符，结构完整


def test_scorer_good_document_passes():
    r = score_document(GOOD_DOC, pre_clean_chars=int(len(GOOD_DOC) * 1.05))
    assert r["score"] >= 80
    assert not any("清洗损失" in x for x in r["reasons"])


def test_scorer_rejects_garbage():
    garbage = "。。。。。。！！！### ？？？？@@@ \t\n" * 60
    r = score_document(garbage)
    assert r["score"] < 60  # 低于默认阈值 → 推送人工审
    assert any("有效文本占比" in x for x in r["reasons"])


def test_scorer_rejects_too_short():
    r = score_document("请假请找上级审批。")
    assert r["score"] < 60
    assert any("过短" in x for x in r["reasons"])


def test_scorer_flags_cleaning_loss():
    r = score_document(GOOD_DOC, pre_clean_chars=len(GOOD_DOC) * 3)
    assert r["score"] < 80
    assert any("清洗损失" in x for x in r["reasons"])


def test_scorer_structure_missing_penalty():
    wall = "这是一段没有任何标题结构的制度内容。" * 100
    r = score_document(wall)
    assert any("结构" in x for x in r["reasons"])


# ---------- 审核存储状态机 ----------

def _score_info(score: int = 50) -> dict:
    return {"score": score, "reasons": ["测试"], "details": {}, "source": ""}


def test_store_status_transitions(tmp_path):
    store = ReviewStore(path=tmp_path / "review.json", raw_dir=tmp_path / "raw")
    rec = store.submit(
        "d1", kb_id="default", filename="a.docx", status="pending_review",
        score_info=_score_info(45), raw_text="raw", clean_text="clean",
    )
    assert rec["status"] == "pending_review"
    assert rec["tier"] == "raw"
    assert (tmp_path / "raw" / "d1.raw.txt").exists()
    assert (tmp_path / "raw" / "d1.clean.txt").exists()

    # 标注
    store.annotate("d1", 0, "关键流程节点", by="admin")
    store.annotate("d1", 0, "更正：关键条款", by="admin")  # 同 index 覆盖
    rec = store.get("d1")
    assert len(rec["annotations"]) == 1
    assert rec["annotations"][0]["note"] == "更正：关键条款"

    # 通过 → approved + highlight
    store.approve("d1", review_score=85, comment="OK", by="admin")
    rec = store.get("d1")
    assert rec["status"] == "approved"
    assert rec["tier"] == "highlight"
    assert rec["review_score"] == 85

    # 退回已生效 → rejected + 降级 raw
    store.reject("d1", reason="内容过期", by="admin")
    rec = store.get("d1")
    assert rec["status"] == "rejected"
    assert rec["tier"] == "raw"
    assert rec["rejected_reason"] == "内容过期"


def test_store_queue_sorted_by_score(tmp_path):
    store = ReviewStore(path=tmp_path / "review.json", raw_dir=tmp_path / "raw")
    for did, sc in (("d-high", 55), ("d-low", 20), ("d-mid", 40)):
        store.submit(did, kb_id="default", filename=f"{did}.txt",
                     status="pending_review", score_info=_score_info(sc))
    ids = [r["doc_id"] for r in store.queue()]
    assert ids == ["d-low", "d-mid", "d-high"]
    assert store.counts()["pending_review"] == 3


def test_store_submit_rejects_bad_initial_status(tmp_path):
    store = ReviewStore(path=tmp_path / "review.json", raw_dir=tmp_path / "raw")
    try:
        store.submit("x", kb_id="k", filename="f", status="rejected", score_info=_score_info())
        raised = False
    except ValueError:
        raised = True
    assert raised


# ---------- 流水线（注入 fake ingest + 临时 store） ----------

class _FakeIngest:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, chunks, doc_id=None, content_hash=None, kb_id="default", filename=None):
        self.calls.append({"chunks": list(chunks), "filename": filename, "kb_id": kb_id})
        return {"ingested": len(chunks), "doc_id": f"final-{len(self.calls)}",
                "source": filename, "skipped": False, "overwritten": False}


def test_pipeline_low_score_goes_pending(tmp_path, monkeypatch):
    fake = _FakeIngest()
    import src.review.pipeline as pl

    monkeypatch.setattr(pl, "ingest_documents", fake)
    monkeypatch.setattr(pl.settings, "review_enabled", True)
    monkeypatch.setattr(pl.settings, "review_mode", "threshold")
    monkeypatch.setattr(pl.settings, "review_score_threshold", 60)

    store = ReviewStore(path=tmp_path / "review.json", raw_dir=tmp_path / "raw")
    garbage = "。。。。。。！！！###" * 60
    docs = [Document(page_content=garbage, metadata={"source": "bad.txt"})]
    chunks = [Document(page_content=garbage[:100], metadata={"source": "bad.txt"})]

    result = process_and_ingest(
        docs, chunks, kb_id="default", filename="bad.txt",
        pre_clean_text=garbage + "清洗前多一些", store=store,
    )
    assert result["pending_review"] is True
    assert result["review"]["score"] < 60
    assert not fake.calls  # 未进向量索引
    rec = store.get(result["doc_id"])
    assert rec["status"] == "pending_review"
    assert rec["tier"] == "raw"


def test_pipeline_good_score_auto_approves(tmp_path, monkeypatch):
    fake = _FakeIngest()
    import src.review.pipeline as pl

    monkeypatch.setattr(pl, "ingest_documents", fake)
    monkeypatch.setattr(pl.settings, "review_enabled", True)
    monkeypatch.setattr(pl.settings, "review_mode", "threshold")
    monkeypatch.setattr(pl.settings, "review_score_threshold", 60)

    store = ReviewStore(path=tmp_path / "review.json", raw_dir=tmp_path / "raw")
    docs = [Document(page_content=GOOD_DOC, metadata={"source": "good.docx"})]
    chunks = [Document(page_content=GOOD_DOC[:200], metadata={"source": "good.docx"})]

    result = process_and_ingest(
        docs, chunks, kb_id="default", filename="good.docx", store=store,
    )
    assert not result.get("pending_review")
    assert result["review"]["status"] == "approved"
    assert len(fake.calls) == 1
    rec = store.get(result["doc_id"])
    assert rec["status"] == "approved"
    assert rec["tier"] == "highlight"


def test_pipeline_disabled_bypasses(tmp_path, monkeypatch):
    fake = _FakeIngest()
    import src.review.pipeline as pl

    monkeypatch.setattr(pl, "ingest_documents", fake)
    monkeypatch.setattr(pl.settings, "review_enabled", False)

    store = ReviewStore(path=tmp_path / "review.json", raw_dir=tmp_path / "raw")
    docs = [Document(page_content="垃圾内容" * 10, metadata={"source": "x.txt"})]
    chunks = [Document(page_content="垃圾内容", metadata={"source": "x.txt"})]
    result = process_and_ingest(docs, chunks, kb_id="default", store=store)
    assert result["review"]["status"] == "bypassed"
    assert len(fake.calls) == 1
    assert store.counts()["approved"] == 0  # 不登记审核记录


def test_approve_rechunks_with_annotations(tmp_path, monkeypatch):
    """过审链路：重切片 → 标注落到分片元数据 → 入库 → 状态 approved。"""
    fake = _FakeIngest()
    import src.review.pipeline as pl

    monkeypatch.setattr(pl, "ingest_documents", fake)
    monkeypatch.setattr(pl.settings, "review_enabled", True)
    monkeypatch.setattr(pl.settings, "review_mode", "threshold")
    monkeypatch.setattr(pl.settings, "review_score_threshold", 60)

    store = ReviewStore(path=tmp_path / "review.json", raw_dir=tmp_path / "raw")
    garbage = "。。。。！！" * 300  # 1800 字符，确保切出多个分片
    store.submit("rev-1", kb_id="default", filename="bad.txt",
                 status="pending_review", score_info=_score_info(30),
                 raw_text=garbage, clean_text=garbage)
    store.annotate("rev-1", 1, "关键条款：赔偿标准", by="admin")

    result = approve_document("rev-1", review_score=70, by="admin", store=store)
    assert result["review"]["status"] == "approved"
    assert len(fake.calls) == 1
    added = fake.calls[0]["chunks"]
    highlighted = [c for c in added if c.metadata.get("highlight")]
    assert len(highlighted) == 1
    assert highlighted[0].metadata["annotation"] == "关键条款：赔偿标准"
    assert store.get("rev-1")["status"] == "approved"
    # 过审后 doc_id 更新为入库返回的真实 ID
    assert store.get("rev-1")["doc_id"] == "final-1"


def test_reject_purges_approved_doc(tmp_path, monkeypatch):
    """退回已生效文档：同步从索引移除。"""
    import src.review.pipeline as pl

    removed: list[str] = []
    monkeypatch.setattr(pl, "remove_document", lambda doc_id, kb_id="default": removed.append(doc_id) or True)

    store = ReviewStore(path=tmp_path / "review.json", raw_dir=tmp_path / "raw")
    store.submit("rev-2", kb_id="default", filename="ok.txt",
                 status="approved", score_info=_score_info(90))
    result = reject_document("rev-2", reason="过期", store=store)
    assert result["purged_from_index"] is True
    assert removed == ["rev-2"]
    assert store.get("rev-2")["status"] == "rejected"
    assert store.get("rev-2")["tier"] == "raw"


# ---------- 检索加权 ----------

def test_rrf_highlight_weighting(monkeypatch):
    """同排名下，带 highlight 标注的分片应排到无标注分片之前。"""
    from src.retrieval import hybrid_search as hs

    d_plain = Document(page_content="普通分片", metadata={"chunk_id": "c1"})
    d_hl = Document(page_content="标注分片", metadata={"chunk_id": "c2", "highlight": True})
    out = hs._rrf_fusion("q", [d_plain, d_hl], [], top_k=2)
    assert out[0].page_content == "标注分片"
    assert out[0].metadata.get("highlighted") is True
    # 加权后分数应高于未加权
    assert out[0].metadata["score"] > out[1].metadata["score"] * (1 + hs.HIGHLIGHT_WEIGHT) / (1 + hs.HIGHLIGHT_WEIGHT) * 0.99


def test_apply_annotations_ignores_out_of_range():
    chunks = [Document(page_content="a"), Document(page_content="b")]
    hits = apply_annotations(chunks, [{"chunk_index": 0, "note": "n1"}, {"chunk_index": 9, "note": "越界"}])
    assert hits == 1
    assert chunks[0].metadata["highlight"] is True
    assert not chunks[1].metadata.get("highlight")
