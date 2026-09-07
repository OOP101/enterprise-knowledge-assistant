"""固定流程办理链（v2.4）：LangChain 结构化起草 → 用户确认 → 固定提交。

为什么不用 Agent 即兴发起：一句话直接 launch 会把原始输入塞进 note 当参数，
请假类型/日期/时长全缺失，用户还要去流程中心改。固定流程改走确定性链路：

    自然语言 → LLM 结构化提取（with_structured_output）→ 表单草稿（含缺失项）
    → 前端确认卡片补全 → /workflow/submit 定向提交 → 工单卡

无 LLM（演示模式）时用规则/正则兜底提取，保证离线可跑。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---- 流程字段 Schema（label/type/options 驱动前端表单渲染） ----
WORKFLOW_FIELD_SCHEMAS: dict[str, dict] = {
    "leave": {
        "name": "请假申请",
        "fields": [
            {"key": "leave_type", "label": "请假类型", "type": "select",
             "options": ["事假", "病假", "年假", "调休", "婚假", "产假", "陪产假", "其他"], "required": True},
            {"key": "start_date", "label": "开始日期", "type": "date", "required": True},
            {"key": "end_date", "label": "结束日期", "type": "date", "required": True},
            {"key": "reason", "label": "事由", "type": "textarea", "required": False},
        ],
    },
    "overtime": {
        "name": "加班申请",
        "fields": [
            {"key": "date", "label": "加班日期", "type": "date", "required": True},
            {"key": "hours", "label": "加班时长（小时）", "type": "number", "required": True},
            {"key": "reason", "label": "加班事由", "type": "textarea", "required": False},
        ],
    },
    "reimbursement": {
        "name": "报销申请",
        "fields": [
            {"key": "category", "label": "报销类别", "type": "select",
             "options": ["差旅费", "办公费", "业务招待费", "培训费", "其他"], "required": True},
            {"key": "amount", "label": "金额（元）", "type": "number", "required": True},
            {"key": "reason", "label": "报销事由", "type": "textarea", "required": False},
        ],
    },
    "onboarding": {
        "name": "入职办理",
        "fields": [
            {"key": "name", "label": "入职人姓名", "type": "text", "required": True},
            {"key": "department", "label": "入职部门", "type": "text", "required": True},
            {"key": "start_date", "label": "入职日期", "type": "date", "required": True},
        ],
    },
}

# 演示模式（无 LLM）的规则兜底提取
_LEAVE_TYPES = ("事假", "病假", "年假", "调休", "婚假", "产假", "陪产假")
_REIM_CATS = ("差旅", "办公", "招待", "培训")


class WorkflowDraft(BaseModel):
    """LLM 结构化提取的结果模型（with_structured_output 用）。"""

    workflow_key: str = Field(description="流程标识：leave/overtime/reimbursement/onboarding 之一")
    params: dict[str, Any] = Field(default_factory=dict, description="从用户话术中提取的参数")


def _normalize_date(value: str) -> str:
    """把「2026年9月1日 / 9月1日 / 明天」等尽量规范为 YYYY-MM-DD，无法解析返回原值。"""
    if not value:
        return ""
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]?", value)
    if m:
        return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"  # 缺年份，前端可补
    return value.strip()


def _regex_extract(text: str, workflow_key: str) -> dict:
    """无 LLM 时的规则提取：日期 / 类型关键词 / 数字（时长、金额）。"""
    params: dict[str, Any] = {}
    dates = re.findall(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?|\d{1,2}月\d{1,2}[日号]?)", text)
    schema = WORKFLOW_FIELD_SCHEMAS.get(workflow_key, {})
    date_keys = [f["key"] for f in schema.get("fields", []) if f["type"] == "date"]
    for i, key in enumerate(date_keys):
        if i < len(dates):
            params[key] = _normalize_date(dates[i])
    for t in _LEAVE_TYPES:
        if t in text:
            params["leave_type"] = t
            break
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:个)?小时", text)
    if m:
        params["hours"] = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
    if m:
        params["amount"] = float(m.group(1))
    for c in _REIM_CATS:
        if c in text:
            params["category"] = c + ("费" if not c.endswith("费") else "")
            break
    return params


def _llm_extract(text: str, workflow_key: str, llm: Any) -> dict:
    """LLM 结构化提取（LangChain with_structured_output），失败返回空 dict。"""
    try:
        import json

        from langchain_core.prompts import ChatPromptTemplate

        schema = WORKFLOW_FIELD_SCHEMAS[workflow_key]
        field_desc = "\n".join(
            f"- {f['key']}（{f['label']}，{'必填' if f['required'] else '选填'}"
            + (f"，可选值：{'/'.join(f['options'])}" if f.get("options") else "")
            + "）"
            for f in schema["fields"]
        )
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "从用户话术中提取流程申请参数。今天是 2026-08-30。只提取话术中明确提到的信息，"
                    "没有提到的字段不要编造、不要放进 params。日期用 YYYY-MM-DD 格式。\n"
                    f"需要提取的字段：\n{field_desc}",
                ),
                ("human", "{text}"),
            ]
        )
        chain = prompt | llm.with_structured_output(WorkflowDraft)
        result: WorkflowDraft = chain.invoke({"text": text})
        return {k: v for k, v in (result.params or {}).items() if v not in (None, "")}
    except Exception as e:  # noqa: BLE001
        logger.debug("LLM 参数提取失败，回退规则提取：%s", e)
        return {}


def build_draft(text: str, workflow_key: str | None = None, llm: Any = None) -> dict:
    """生成流程草稿：schema + 已提取参数 + 缺失必填项。"""
    if not workflow_key or workflow_key not in WORKFLOW_FIELD_SCHEMAS:
        # 从文本猜流程（复用 agent 的关键词映射）
        from src.agent.agent import _pick_workflow

        workflow_key = _pick_workflow(text)
    schema = WORKFLOW_FIELD_SCHEMAS[workflow_key]

    params = _regex_extract(text, workflow_key)
    if llm is not None and getattr(llm, "with_structured_output", None):
        llm_params = _llm_extract(text, workflow_key, llm)
        params.update(llm_params)

    fields = []
    missing = []
    for f in schema["fields"]:
        value = params.get(f["key"], "")
        fields.append({**f, "value": value})
        if f["required"] and not value:
            missing.append(f["label"])
    return {
        "workflow_key": workflow_key,
        "workflow_name": schema["name"],
        "fields": fields,
        "missing": missing,
        "note": text,
    }


def submit_workflow(workflow_key: str, params: dict) -> dict:
    """校验必填后定向提交（固定步骤，不经 Agent 即兴发挥）。"""
    schema = WORKFLOW_FIELD_SCHEMAS.get(workflow_key)
    if not schema:
        return {"launched": False, "error": f"未知流程：{workflow_key}"}
    missing = [
        f["label"] for f in schema["fields"]
        if f["required"] and not str(params.get(f["key"], "")).strip()
    ]
    if missing:
        return {"launched": False, "error": f"缺少必填项：{'、'.join(missing)}", "missing": missing}
    from src.agent.tools import launch_workflow

    result = launch_workflow(workflow_key, params)
    result["workflow_name"] = schema["name"]
    return result
