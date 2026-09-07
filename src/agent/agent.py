"""主 Agent（工具调用闭环，2.0）。

基于 LangGraph 构建有状态工作流：
    classify(意图分类) → route(条件路由) → knowledge/action/list 节点 → memory_writer → END

相比 1.0（route→act 两节点）：
- 显式区分知识问答 / 工具调用 / 流程列表 三个执行节点
- 接入长期记忆写入（action 类流程结果沉淀）
- 意图分类升级为 LLM + 规则双保险（规则兜底保证离线）

意图分类：query(知识问答) / action(流程办理) / list(流程列表)。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from config.settings import settings
from src.agent.tools import TOOL_REGISTRY, list_workflows, query_knowledge

# 流程动作关键词
_ACTION_WORDS = {
    "请假": "leave",
    "加班": "overtime",
    "报销": "reimbursement",
    "入职": "onboarding",
    "办理": "launch",
}
# 流程列表触发词
_LIST_WORDS = ("有哪些流程", "流程列表", "什么流程", "怎么办理", "有哪流程", "都有哪些流程", "流程有哪些")


def classify_intent(user_input: str) -> str:
    """基于规则做意图分类（离线可用）。

    优先级：list > query(疑问词) > action(行动词)。
    """
    if any(k in user_input for k in _LIST_WORDS):
        return "list"
    if any(k in user_input for k in ("什么", "怎么", "哪些", "如何", "需要什么", "材料", "要求", "是什么", "多少")):
        return "query"
    for word in _ACTION_WORDS:
        if word in user_input and any(
            k in user_input for k in ("申请", "发起", "提交", "办理", "帮我", "去办", "给我办")
        ):
            return "action"
    return "query"


# LLM 判定 action 时的强动作表达词（防止把"咨询流程"误判为"发起申请"）
_ACTION_MARKERS = ("申请", "发起", "提交", "办理", "帮我", "去办", "给我办", "我要办", "请帮我", "帮我办")
# 疑问/咨询语义提示词：即便含动作词，也说明用户是在咨询而非发起
_QUERY_HINTS = ("怎么", "如何", "怎样", "什么", "哪些", "多少", "是否", "流程", "规则", "制度", "要求", "材料", "条件")


def _refine_llm_intent(llm_intent: str, user_input: str) -> str:
    """用规则对 LLM 意图做后置校验：LLM 判 action 但无动作表达时降级为 query。

    实测 DeepSeek 会把"请假流程 / 请假怎么办理 / 请假需要什么材料"等纯咨询问题
    一律判成 action，导致前端跳过流式直接整段渲染。这里用规则兜底校正。
    """
    if llm_intent == "action":
        if any(h in user_input for h in _QUERY_HINTS):
            # 带疑问/咨询语义：即使有"办理"也视为咨询而非发起
            if any(k in user_input for k in _LIST_WORDS):
                return "list"
            return "query"
        if not any(m in user_input for m in _ACTION_MARKERS):
            return "query"
    if llm_intent == "query" and any(k in user_input for k in _LIST_WORDS):
        return "list"
    return llm_intent


def classify_intent_with_llm(user_input: str, llm: Any) -> str:
    """LLM 意图分类（有 Key 时更准），失败回退规则。"""
    if llm is None or not getattr(llm, "invoke", None):
        return classify_intent(user_input)
    try:
        from src.prompts.templates import intent_prompt

        prompt = intent_prompt.invoke({"question": user_input})
        resp = llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        text = text.strip().lower()
        if text in ("query", "action", "list"):
            return _refine_llm_intent(text, user_input)
    except Exception:  # noqa: BLE001
        pass
    return classify_intent(user_input)


def _pick_workflow(user_input: str) -> str:
    for word, key in _ACTION_WORDS.items():
        if word in user_input:
            return key
    return "leave"


def _execute(intent: str, user_input: str, session_id: str = "default", kb_id: str = "default") -> dict:
    if intent == "list":
        result = list_workflows()
        answer = "当前可用的流程如下：\n" + "\n".join(
            f"- {w['name']}（{w['description']}）" for w in result["workflows"]
        )
        return {
            "intent": intent,
            "answer": answer,
            "action": None,
            "workflows": result["workflows"],
            "session_id": session_id,
            "kb_id": kb_id,
        }
    if intent == "action":
        # v2.4：不再一句话直接开单（参数全缺失），改为返回结构化草稿，
        # 由前端渲染确认表单，用户补全后走 /api/qa/workflow/submit 定向提交。
        from src.agent.workflow_chain import build_draft

        draft = build_draft(user_input, llm=None)  # 规则提取，零延迟；表单可补全
        answer = (
            f"好的，我来协助你发起【{draft['workflow_name']}】。"
            + (
                "已识别以下信息，请确认或补全后提交："
                if draft["fields"]
                else "请填写以下信息后提交："
            )
        )
        return {
            "intent": intent,
            "answer": answer,
            "action": None,
            "draft": draft,
            "session_id": session_id,
            "kb_id": kb_id,
        }
    # query（限定知识库）
    kb = query_knowledge(user_input, kb_id=kb_id)
    answer = kb["answer"] if kb["found"] else "知识库中暂未检索到相关内容，请补充关键词或联系管理员导入文档。"
    return {"intent": intent, "answer": answer, "action": None, "session_id": session_id, "kb_id": kb_id}


def _persist_action_memory(session_id: str, user_input: str, res: dict) -> None:
    """将流程发起动作沉淀为长期记忆。"""
    try:
        from src.core import get_container

        vm = get_container().get_vector_memory()
        vm.remember(
            session_id,
            "user",
            f"用户发起了【{res['workflow']}】申请，工单号 {res['ticket_id']}（原话：{user_input}）",
        )
    except Exception:  # noqa: BLE001
        pass


class SimpleAgent:
    """内置简易 Agent（LangGraph 未安装时的兜底）。"""

    def invoke(self, user_input: str, session_id: str = "default", kb_id: str = "default") -> dict:
        intent = classify_intent(user_input)
        return _execute(intent, user_input, session_id=session_id, kb_id=kb_id)


# ---- LangGraph 状态与节点（模块级定义，避免类型注解在函数作用域无法解析）----
try:  # 延迟导入类型，避免顶层强依赖 langgraph
    from typing_extensions import TypedDict  # type: ignore

    class AgentState(TypedDict):
        input: str
        session_id: str
        intent: str
        output: dict
        kb_id: str

except Exception:  # pragma: no cover
    AgentState = dict  # type: ignore


def _classify_node(state: dict) -> dict:
    """意图分类节点：LLM + 规则双保险（LLM 失败/离线自动回退规则）。"""
    llm = None
    try:
        from src.models.llm import get_llm

        llm = get_llm()
    except Exception:  # noqa: BLE001
        llm = None
    state["intent"] = classify_intent_with_llm(state["input"], llm)
    return state


def _kb_id(state: dict) -> str:
    return state.get("kb_id", "default")


def _knowledge_node(state: dict) -> dict:
    state["output"] = _execute("query", state["input"], session_id=state.get("session_id", "default"), kb_id=_kb_id(state))
    return state


def _action_node(state: dict) -> dict:
    state["output"] = _execute("action", state["input"], session_id=state.get("session_id", "default"), kb_id=_kb_id(state))
    return state


def _list_node(state: dict) -> dict:
    state["output"] = _execute("list", state["input"], session_id=state.get("session_id", "default"), kb_id=_kb_id(state))
    return state


def _route_intent(state: dict) -> str:
    return {"query": "knowledge", "action": "action", "list": "list"}.get(
        state.get("intent", "query"), "knowledge"
    )


def _build_langgraph_agent():
    """构建 LangGraph 多节点工作流（需安装 langgraph）。"""
    from langgraph.graph import END, StateGraph  # type: ignore

    graph = StateGraph(AgentState)
    graph.add_node("classify", _classify_node)
    graph.add_node("knowledge", _knowledge_node)
    graph.add_node("action", _action_node)
    graph.add_node("list", _list_node)
    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", _route_intent, {
        "knowledge": "knowledge",
        "action": "action",
        "list": "list",
    })
    graph.add_edge("knowledge", END)
    graph.add_edge("action", END)
    graph.add_edge("list", END)
    return graph.compile()


@lru_cache
def get_agent() -> Any:
    """返回主 Agent（LangGraph 优先，SimpleAgent 兜底）。"""
    try:
        return _build_langgraph_agent()
    except ImportError:
        return SimpleAgent()


def run_agent(user_input: str, session_id: str = "default", kb_id: str = "default") -> dict:
    """对外统一入口。"""
    agent = get_agent()
    try:
        result = agent.invoke({"input": user_input, "session_id": session_id, "kb_id": kb_id})
        if isinstance(result, dict) and "output" in result:
            return result["output"]
        return result
    except (TypeError, AttributeError):
        # SimpleAgent 的 invoke 签名不同
        return agent.invoke(user_input, session_id=session_id, kb_id=kb_id)


def clean_answer(answer: str) -> str:
    """清洗回答中的标记（如 [来源]）。"""
    return re.sub(r"\[来源:.*?\]", "", answer).strip()
