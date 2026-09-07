"""LLM 自主工具调用 Agent（ReAct 风格，3.0 前瞻）。

与 2.0 的规则意图分类 Agent（``agent.py``）不同，本 Agent 把工具选择权交给 LLM：

- 每轮由 LLM 判断：**直接回答**，还是**调用某个工具**（来自 ``TOOL_REGISTRY``）
- 支持多轮、多工具组合（如「先查知识库，再发起流程」）
- 配置了真实 LLM（支持 function calling）时走 ReAct 循环；
  离线 / 演示模式（DemoLLM）自动回退规则 Agent，保证无 Key 链路可用

图结构（LangGraph）::

    agent（LLM 推理，bind_tools） ⇄  tools（执行工具调用） → finish → END

调用示例::

    from src.agent.react_agent import run_react_agent
    result = run_react_agent("我想请 3 天假", kb_id="default")
    # -> {"intent": "agent", "answer": "...", "trace": [...], "mode": "llm" | "rule"}
"""
from __future__ import annotations

import json
import logging
from typing import Any

from config.settings import settings
from src.agent.agent import run_agent
from src.agent.tools import launch_workflow, list_workflows, query_knowledge
from src.core import get_container
from src.models.llm import get_llm

logger = logging.getLogger(__name__)

# ReAct 循环最大轮数（防止 LLM 无限调用工具）
MAX_ITERATIONS = 4


# ---- 工具参数 Schema（供 LLM function calling 生成调用） ----
from pydantic import BaseModel, Field


class QueryKnowledgeInput(BaseModel):
    """query_knowledge 工具参数。"""

    query: str = Field(..., description="要检索的企业知识库问题或关键词")
    top_k: int = Field(default=3, ge=1, le=10, description="返回文档条数")


class LaunchWorkflowInput(BaseModel):
    """launch_workflow 工具参数。"""

    workflow_name: str = Field(
        ..., description="流程名称：leave(请假) / overtime(加班) / reimbursement(报销) / onboarding(入职)"
    )
    params: dict = Field(default_factory=dict, description="流程参数，如备注说明")


class ListWorkflowsInput(BaseModel):
    """list_workflows 工具参数（无参数）。"""


def build_tools(kb_id: str = "default") -> list[Any]:
    """从 TOOL_REGISTRY 构造 LangChain StructuredTool 列表（kb_id 绑定到 query_knowledge）。

    工具结果统一转为 JSON 字符串返回给 LLM（function calling 的惯例）。
    """
    from langchain_core.tools import StructuredTool

    def _query(query: str, top_k: int = 3) -> str:
        result = query_knowledge(query, top_k=top_k, kb_id=kb_id)
        return json.dumps(result, ensure_ascii=False)

    def _launch(workflow_name: str, params: dict | None = None) -> str:
        result = launch_workflow(workflow_name, params or {})
        return json.dumps(result, ensure_ascii=False)

    def _list() -> str:
        return json.dumps(list_workflows(), ensure_ascii=False)

    return [
        StructuredTool.from_function(
            func=_query,
            name="query_knowledge",
            description=(
                "检索企业知识库（SOP / 制度 / 流程文档），回答知识类问题。"
                '返回 JSON：{"found": bool, "answer": str, "sources": [...]}'
            ),
            args_schema=QueryKnowledgeInput,
        ),
        StructuredTool.from_function(
            func=_launch,
            name="launch_workflow",
            description=(
                "发起流程申请（请假/加班/报销/入职），创建工单。"
                '返回 JSON：{"launched": bool, "workflow": str, "ticket_id": str, "url": str, "mode": str}'
            ),
            args_schema=LaunchWorkflowInput,
        ),
        StructuredTool.from_function(
            func=_list,
            name="list_workflows",
            description="列出所有可用的流程服务。返回 JSON：{\"workflows\": [{key, name, description}]}",
            args_schema=ListWorkflowsInput,
        ),
    ]


def build_system_prompt(
    tools: list[Any],
    long_memory: list[str] | None = None,
    history: list[Any] | None = None,
) -> str:
    """组装 Agent 系统提示词：工具清单 + 长期记忆 + 历史对话。"""
    from src.prompts.templates import AGENT_SYSTEM

    tool_lines = "\n".join(f"- {t.name}：{t.description}" for t in tools)
    system = AGENT_SYSTEM.format(tools=tool_lines)
    if long_memory:
        system += "\n\n【长期记忆】\n" + "\n".join(f"- {m}" for m in long_memory)
    if history:
        history_text = "\n".join(
            f"{m.type}: {getattr(m, 'content', '')}" for m in history[-settings.memory_k * 2 :]
        )
        system += "\n\n【历史对话】\n" + history_text
    return system


def _build_graph(llm: Any, kb_id: str, max_iterations: int):
    """构建 LangGraph 循环：agent（LLM 推理）⇄ tools（执行工具）→ finish。"""
    from langgraph.graph import END, StateGraph

    try:
        from typing_extensions import TypedDict
    except ImportError:  # pragma: no cover
        from typing import TypedDict

    tools = build_tools(kb_id)
    tool_map = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    class ReActState(TypedDict, total=False):
        messages: list
        iterations: int
        max_iterations: int
        answer: str
        trace: list
        sources: list

    def _agent_node(state: dict) -> dict:
        """LLM 推理：根据当前消息决定直接回答或发起工具调用。"""
        resp = llm_with_tools.invoke(state["messages"])
        return {
            "messages": state["messages"] + [resp],
            "iterations": state.get("iterations", 0) + 1,
        }

    def _tools_node(state: dict) -> dict:
        """执行上一条 AIMessage 中的所有工具调用，结果以 ToolMessage 回填。"""
        last = state["messages"][-1]
        trace = list(state.get("trace", []))
        sources = list(state.get("sources", []))
        appended: list[Any] = []

        for tc in getattr(last, "tool_calls", []) or []:
            name = tc.get("name", "")
            args = tc.get("args", {}) or {}
            tool = tool_map.get(name)
            if tool is None:
                content = json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
            else:
                content = tool.invoke(args)

            trace.append({"tool": name, "args": args, "result": content})

            # 知识检索工具的结果里提取 sources，供最终回答引用
            if name == "query_knowledge":
                try:
                    data = json.loads(content)
                    sources.extend(data.get("sources", []) or [])
                except Exception:  # noqa: BLE001
                    pass

            from langchain_core.messages import ToolMessage

            appended.append(
                ToolMessage(content=str(content), tool_call_id=tc.get("id", ""))
            )

        return {
            "messages": state["messages"] + appended,
            "trace": trace,
            "sources": sources,
        }

    def _finish_node(state: dict) -> dict:
        """取最后一条 AIMessage 的 content 作为最终回答。"""
        answer = ""
        for msg in reversed(state["messages"]):
            if getattr(msg, "type", "") == "ai" and getattr(msg, "content", ""):
                answer = msg.content
                break
        if not answer:
            answer = (
                "已执行多次工具调用但未生成最终回答。已调用："
                + json.dumps(state.get("trace", []), ensure_ascii=False)
            )
        return {"answer": answer}

    def _should_continue(state: dict) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state.get("iterations", 0) < state.get(
            "max_iterations", max_iterations
        ):
            return "tools"
        return "finish"

    graph = StateGraph(ReActState)
    graph.add_node("agent", _agent_node)
    graph.add_node("tools", _tools_node)
    graph.add_node("finish", _finish_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent", _should_continue, {"tools": "tools", "finish": "finish"}
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("finish", END)
    return graph.compile(), tools


def _rule_fallback(user_input: str, session_id: str, kb_id: str) -> dict:
    """离线 / 异常兜底：复用 2.0 规则 Agent（无 Key 也能跑）。"""
    result = run_agent(user_input, session_id=session_id, kb_id=kb_id)
    result["mode"] = "rule"
    result["trace"] = []
    result.setdefault("sources", [])
    return result


def run_react_agent(
    user_input: str,
    session_id: str = "default",
    kb_id: str = "default",
    llm: Any | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> dict:
    """LLM 自主工具调用 Agent 的统一入口。

    返回结构（与 ``run_agent`` 对齐，便于 API 层复用）::

        {
            "intent": "agent",
            "answer": str,
            "sources": [{"source", "content"}],
            "trace": [{"tool", "args", "result"}],
            "action": None,
            "session_id": str,
            "kb_id": str,
            "mode": "llm" | "rule",   # llm=走 ReAct 循环；rule=离线回退
        }

    - 真实 LLM 支持 function calling（``bind_tools``）时走 ReAct 循环；
    - 演示模式（DemoLLM）或任何异常自动回退规则 Agent。
    """
    llm = llm or get_llm()
    if not callable(getattr(llm, "bind_tools", None)):
        logger.debug("LLM 不支持工具调用（演示模式），回退规则 Agent")
        return _rule_fallback(user_input, session_id, kb_id)

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        container = get_container()
        memory = container.get_session_memory()
        history = memory.get_history(session_id)

        # 长期向量记忆注入
        long_memory: list[str] = []
        try:
            long_memory = container.get_vector_memory().recall(
                user_input, top_k=settings.long_memory_top_k
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("长期记忆注入失败：%s", e)

        graph, tools = _build_graph(llm, kb_id, max_iterations)
        system = build_system_prompt(tools, long_memory, history)
        messages = [
            SystemMessage(content=system),
            *history,
            HumanMessage(content=user_input),
        ]

        state = graph.invoke(
            {
                "messages": messages,
                "iterations": 0,
                "max_iterations": max_iterations,
                "trace": [],
                "sources": [],
            }
        )
        answer = state.get("answer", "")

        # 记录会话记忆（供多轮对话）
        try:
            memory.add_turn(session_id, user_input, answer)
        except Exception as e:  # noqa: BLE001
            logger.debug("会话记忆写入失败：%s", e)

        return {
            "intent": "agent",
            "answer": answer,
            "sources": state.get("sources", []),
            "trace": state.get("trace", []),
            "action": None,
            "session_id": session_id,
            "kb_id": kb_id,
            "mode": "llm",
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("ReAct Agent 执行失败，回退规则 Agent：%s", e)
        return _rule_fallback(user_input, session_id, kb_id)
