"""评估执行器：加载评估集 → 跑检索/问答 → 计算指标。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config.settings import settings
from src.eval.metrics import f1, hit_rate, llm_judge, precision

logger = logging.getLogger(__name__)

DEFAULT_EVAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "eval"


def load_dataset(path: Path | None = None) -> list[dict]:
    """加载评估集（data/eval/*.json）。

    path 可为目录（扫描全部 *.json）或单个文件（直接读取）。
    """
    eval_path = path or DEFAULT_EVAL_DIR
    files: list[Path] = []
    if eval_path.is_file():
        files = [eval_path]
    elif eval_path.is_dir():
        files = sorted(eval_path.glob("*.json"))
    items: list[dict] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                items.append(data)
        except Exception as e:  # noqa: BLE001
            logger.warning("评估集文件解析失败 %s：%s", f, e)
    return items


def run_eval(
    retriever: Any,
    llm: Any | None = None,
    use_judge: bool = False,
    kb_id: str = "default",
    dataset_path: Path | None = None,
) -> dict:
    """执行评估，返回汇总指标与逐条明细。"""
    items = load_dataset(dataset_path)
    if not items:
        return {"total": 0, "hit_rate": 0.0, "precision": 0.0, "f1": 0.0, "cases": []}

    results = []
    for case in items:
        question = case.get("question", "")
        gold_docs = case.get("gold_docs", [])
        gold_answer = case.get("gold_answer", "")

        docs = retriever.retrieve(question, top_k=settings.eval_top_k)
        recalled = [
            _basename(getattr(d, "metadata", {}).get("source", "")) for d in docs
        ]

        case_result = {
            "question": question,
            "hit_rate": hit_rate(recalled, gold_docs),
            "precision": precision(recalled, gold_docs),
            "f1": f1(recalled, gold_docs),
            "recalled": recalled,
            "gold": gold_docs,
        }

        if use_judge and llm is not None and gold_answer:
            answer = _generate_answer(llm, question, docs)
            case_result["judge"] = llm_judge(llm, question, answer, gold_answer)
            case_result["answer"] = answer

        results.append(case_result)

    n = len(results)
    return {
        "total": n,
        "hit_rate": round(sum(r["hit_rate"] for r in results) / n, 4),
        "precision": round(sum(r["precision"] for r in results) / n, 4),
        "f1": round(sum(r["f1"] for r in results) / n, 4),
        "cases": results,
    }


def _basename(source: str) -> str:
    """归一化来源为文件名（评估集 gold_docs 使用 basename，入库 source 为完整路径）。"""
    return source.replace("\\", "/").rsplit("/", 1)[-1] if source else ""


def _generate_answer(llm: Any, question: str, docs: list) -> str:
    context = "\n\n".join(
        f"[{i + 1}] {getattr(d, 'page_content', str(d))}" for i, d in enumerate(docs)
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from src.prompts.templates import QA_SYSTEM

        prompt = [
            SystemMessage(content=QA_SYSTEM.format(context=context)),
            HumanMessage(content=question),
        ]
        resp = llm.invoke(prompt)
        return resp.content if hasattr(resp, "content") else str(resp)
    except (TypeError, AttributeError):
        return _extract_plain(llm.invoke(question))


def _extract_plain(answer: Any) -> str:
    if isinstance(answer, str):
        return answer
    content = getattr(answer, "content", answer)
    return str(content) if content is not None else ""
