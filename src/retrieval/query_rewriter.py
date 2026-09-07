"""查询改写模块。

将用户口语化问题改写为检索友好的表述，提升召回率：
1. LLM 改写：调用 LLM 将口语化问题扩展为多角度检索关键词
2. 规则改写：离线兜底，基于同义词表与关键词扩展

项目文档 v1.5 规划：「Query改写 → 将口语化问题改写为检索友好的表述」
"""
from __future__ import annotations

import logging
import re
from typing import Any

from config.settings import settings
from src.models.llm import get_llm

logger = logging.getLogger(__name__)

# 离线同义词扩展表（演示模式兜底）
_SYNONYM_MAP: dict[str, list[str]] = {
    "请假": ["事假", "病假", "年假", "调休", "休假", "请假流程", "请假制度"],
    "报销": ["费用报销", "差旅报销", "采购报销", "报销流程", "报销制度"],
    "加班": ["加班申请", "加班费", "加班时长", "加班制度"],
    "入职": ["新员工入职", "入职手续", "入职流程", "新员工"],
    "离职": ["辞职", "离职手续", "离职流程", "辞职申请"],
    "采购": ["采购流程", "采购制度", "采购管理", "采购合同"],
    "仓库": ["仓库管理", "库存管理", "出入库", "仓库制度"],
    "保密": ["保密协议", "保密制度", "机密", "信息安全"],
    "考勤": ["打卡", "出勤", "考勤制度", "迟到", "早退"],
    "会议": ["会议制度", "高效会议", "会议记录", "会议纪要"],
}

# 口语化 → 书面化映射
_COLLOQUIAL_MAP: dict[str, str] = {
    "怎么": "如何",
    "咋": "如何",
    "啥": "什么",
    "哪有": "哪里",
    "多少天": "几天",
    "能不能": "是否可以",
    "可不可以": "是否可以",
    "搞": "办理",
    "弄": "办理",
    "整": "办理",
}


def _rule_rewrite(query: str) -> str:
    """规则改写：口语化→书面化 + 同义词扩展。"""
    result = query
    for colloquial, formal in _COLLOQUIAL_MAP.items():
        result = result.replace(colloquial, formal)

    expansions: list[str] = []
    for keyword, synonyms in _SYNONYM_MAP.items():
        if keyword in result:
            expansions.extend(synonyms[:3])

    if expansions:
        result = f"{result} {' '.join(expansions[:5])}"

    return result.strip()


def _llm_rewrite(query: str, llm: Any) -> str:
    """LLM 改写：将口语化问题扩展为检索友好的多角度表述。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "你是查询改写助手。将用户的口语化问题改写为更适合检索的表述。\n"
        "要求：\n"
        "1. 补充同义词和相关关键词（如'请假'补充'事假/病假/年假/调休'）\n"
        "2. 将口语转为书面语（如'咋请假'改为'如何申请请假'）\n"
        "3. 只输出改写后的问题，不要输出其他内容\n"
        "4. 改写后的问题应在 30 字以内"
    )
    prompt = [SystemMessage(content=system), HumanMessage(content=query)]
    resp = llm.invoke(prompt)
    text = resp.content if hasattr(resp, "content") else str(resp)
    text = text.strip()
    return text if text else query


def rewrite_query(query: str, llm: Any | None = None) -> str:
    """查询改写入口。

    优先使用 LLM 改写（更智能），未配置时回退规则改写（离线可用）。
    改写后的问题用于向量检索与 BM25 检索，提升召回率。
    """
    if not query or not query.strip():
        return query

    if not settings.query_rewrite:
        return query.strip()

    query = query.strip()

    if llm is None:
        llm = get_llm()

    if settings.llm_ready:
        try:
            rewritten = _llm_rewrite(query, llm)
            logger.debug("LLM 查询改写：%s → %s", query, rewritten)
            return rewritten
        except Exception as e:
            logger.debug("LLM 查询改写失败，回退规则：%s", e)

    return _rule_rewrite(query)
