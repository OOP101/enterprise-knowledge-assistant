"""业务工具（工具调用闭环，2.0）。

从"只给答案"走向"知识+行动"一体化。
- 支持 mock（默认，离线可跑）与真实 HTTP 工具（通过 settings.workflow_api_base 映射到业务系统）
- TOOL_REGISTRY 为结构化定义（含 name/description/args），便于 Agent 与 API 层复用
"""
from __future__ import annotations

import logging
from typing import Any

from config.settings import settings
from src.retrieval.retriever import search_with_context

logger = logging.getLogger(__name__)


def query_knowledge(query: str, top_k: int = 3, kb_id: str = "default") -> dict:
    """知识库检索工具：检索企业内部 SOP / 技术文档（可按知识库隔离）。"""
    context, docs = search_with_context(query, top_k=top_k, kb_id=kb_id)
    return {
        "found": len(docs) > 0,
        "answer": context,
        "sources": [
            {"source": getattr(d, "metadata", {}).get("source", ""), "content": getattr(d, "page_content", "")}
            for d in docs
        ],
    }


def launch_workflow(workflow_name: str, params: dict[str, Any] | None = None) -> dict:
    """发起流程工具：跳转到对应流程中心并创建工单/提交。

    若配置了 workflow_api_base，则调用真实业务系统 API；否则返回 mock 工单（离线演示）。
    """
    params = params or {}
    # 真实 HTTP 工具模式
    if settings.workflow_api_base:
        return _http_launch(workflow_name, params)
    return _mock_launch(workflow_name, params)


def _http_launch(workflow_name: str, params: dict) -> dict:
    """调用真实流程引擎 API（POST {base}/workflows/{name}/launch）。"""
    import json
    import urllib.request

    url = f"{settings.workflow_api_base.rstrip('/')}/workflows/{workflow_name}/launch"
    body = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.workflow_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "launched": True,
                "workflow": workflow_name,
                "url": data.get("url"),
                "ticket_id": data.get("ticket_id", ""),
                "params": params,
                "mode": "http",
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("真实流程 API 调用失败，回退 mock：%s", e)
        result = _mock_launch(workflow_name, params)
        result["fallback_reason"] = str(e)
        return result


def _mock_launch(workflow_name: str, params: dict) -> dict:
    """演示模式：返回伪造工单号。"""
    import uuid

    workflow_map = {
        "leave": {"name": "请假流程", "url": "/workflow/leave"},
        "overtime": {"name": "加班申请", "url": "/workflow/overtime"},
        "reimbursement": {"name": "报销流程", "url": "/workflow/reimbursement"},
        "onboarding": {"name": "入职办理", "url": "/workflow/onboarding"},
    }
    info = workflow_map.get(workflow_name, {"name": workflow_name, "url": None})
    # 使用 uuid 而非内置 hash()（后者跨进程随机，工单号不稳定）
    ticket_id = f"WK-{uuid.uuid4().hex[:10].upper()}"
    return {
        "launched": True,
        "workflow": info["name"],
        "url": info["url"],
        "ticket_id": ticket_id,
        "params": params,
        "mode": "mock",
    }


def list_workflows() -> dict:
    """列出所有可用流程。"""
    return {
        "workflows": [
            {"key": "leave", "name": "请假流程", "description": "事假/病假/年假申请",
             "doc_query": "请假流程 请假制度 事假 病假 年假 申请 审批", "kb_id": "hr"},
            {"key": "overtime", "name": "加班申请", "description": "加班时长申报",
             "doc_query": "加班申请 加班时长 加班费 审批", "kb_id": "hr"},
            {"key": "reimbursement", "name": "报销流程", "description": "差旅/采购报销",
             "doc_query": "报销流程 差旅 采购 报销 制度", "kb_id": "finance"},
            {"key": "onboarding", "name": "入职办理", "description": "新员工入职手续",
             "doc_query": "入职办理 新员工 入职手续", "kb_id": "hr"},
        ]
    }


def get_workflow_doc(key: str, kb_id: str | None = None) -> dict:
    """获取指定流程对应的制度原文（从对应部门知识库检索）。

    kb_id 缺省时使用流程定义里的目标库（如请假→hr、报销→finance）。
    """
    wf = next((w for w in list_workflows()["workflows"] if w["key"] == key), None)
    if wf is None:
        return {"found": False, "workflow": key, "answer": "", "sources": []}
    target = kb_id or wf.get("kb_id", "default")
    result = query_knowledge(wf["doc_query"], top_k=3, kb_id=target)

    # 部门库不存在或未命中时回退默认库（语料未按部门分库时依然能找到原文）
    from config.settings import settings
    from src.kb.manager import get_kb_manager

    fallback = settings.default_kb_id
    if target != fallback and (
        get_kb_manager().get(target) is None or not result["found"]
    ):
        fb_result = query_knowledge(wf["doc_query"], top_k=3, kb_id=fallback)
        if fb_result["found"]:
            target, result = fallback, fb_result
    return {
        "found": result["found"],
        "workflow": wf["name"],
        "key": key,
        "kb_id": target,
        "answer": result["answer"],
        "sources": result["sources"],
    }


# 结构化工具注册表（name → {func, description, args}）
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "query_knowledge": {
        "func": query_knowledge,
        "description": "检索企业知识库，回答 SOP / 制度 / 流程相关提问",
        "args": {"query": "str", "top_k": "int"},
    },
    "launch_workflow": {
        "func": launch_workflow,
        "description": "发起流程申请（请假/加班/报销/入职），创建工单",
        "args": {"workflow_name": "str", "params": "dict"},
    },
    "list_workflows": {
        "func": list_workflows,
        "description": "列出所有可用的流程服务",
        "args": {},
    },
}
