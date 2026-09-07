"""评估指标计算：检索命中率 / 精确率 / LLM-as-judge 打分。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---- 检索指标 ----
def hit_rate(recalled: list[str], gold: list[str]) -> float:
    """命中率：召回文档与标准答案文档的交集 / 标准文档数。

    recalled: 检索返回的 doc_id / source 列表
    gold: 评估集中标注的期望文档列表
    """
    if not gold:
        return 0.0
    gold_set = set(gold)
    recalled_set = set(recalled)
    hit = len(gold_set & recalled_set)
    return round(hit / len(gold_set), 4)


def precision(recalled: list[str], gold: list[str]) -> float:
    """精确率：召回中命中比例。"""
    if not recalled:
        return 0.0
    gold_set = set(gold)
    hit = sum(1 for r in recalled if r in gold_set)
    return round(hit / len(recalled), 4)


def f1(recalled: list[str], gold: list[str]) -> float:
    h = hit_rate(recalled, gold)
    p = precision(recalled, gold)
    if h + p == 0:
        return 0.0
    return round(2 * h * p / (h + p), 4)


# ---- LLM-as-judge ----
_JUDGE_PROMPT = (
    "你是 RAG 评估裁判。根据标准答案评估模型回答的正确性与引用质量，"
    "输出 JSON：{{\"correctness\": 0-5, \"faithfulness\": 0-5, \"comment\": \"...\"}}\n"
    "标准答案：{gold}\n"
    "模型回答：{answer}"
)


def llm_judge(llm: Any, question: str, answer: str, gold_answer: str) -> dict:
    """用 LLM 给回答打分（0-5）。无真实 LLM 时返回保守兜底。"""
    if llm is None or not getattr(llm, "invoke", None):
        return {
            "correctness": 3.0,
            "faithfulness": 3.0,
            "comment": "未配置真实 LLM，采用中性分",
        }
    try:
        from src.prompts.templates import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_template(_JUDGE_PROMPT).invoke(
            {"gold": gold_answer, "answer": answer}
        )
        resp = llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        import json
        import re

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return {
                "correctness": float(data.get("correctness", 3)),
                "faithfulness": float(data.get("faithfulness", 3)),
                "comment": data.get("comment", ""),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM judge 失败：%s", e)
    return {"correctness": 0.0, "faithfulness": 0.0, "comment": "评估失败"}
