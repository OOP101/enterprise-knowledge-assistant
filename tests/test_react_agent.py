"""ReAct Agent（LLM 自主工具调用）测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage

from src.agent.react_agent import build_tools, run_react_agent
from src.models.llm import DemoLLM


class ScriptedToolLLM:
    """脚本化工具调用 LLM 替身：bind_tools 返回自身，invoke 依次弹出脚本响应。"""

    def __init__(self, script: list[AIMessage]) -> None:
        self._script = list(script)
        self.tools = []

    def bind_tools(self, tools):
        self.tools = list(tools)
        return self

    def invoke(self, messages):
        if self._script:
            return self._script.pop(0)
        return AIMessage(content="（脚本耗尽，无更多响应）")


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


# ---- 离线兜底 ----

def test_react_agent_fallback_offline_demo_llm():
    """未配置真实 LLM（DemoLLM 无 bind_tools）时自动回退规则 Agent。"""
    result = run_react_agent(
        "介绍一下请假流程", session_id="t-fallback", kb_id="default", llm=DemoLLM()
    )
    assert result["mode"] == "rule"
    assert result["intent"] in ("query", "action", "list")
    assert "answer" in result
    assert result["trace"] == []


# ---- LLM 工具调用循环 ----

def test_react_agent_llm_calls_tool_then_answers():
    """LLM 先调用 list_workflows 工具，拿到结果后再生成最终回答。"""
    script = [
        _tool_call("list_workflows", {}, "call-1"),
        AIMessage(content="当前可用流程：请假、加班、报销、入职。"),
    ]
    result = run_react_agent(
        "有哪些流程", session_id="t-loop", kb_id="default", llm=ScriptedToolLLM(script)
    )
    assert result["mode"] == "llm"
    assert "请假" in result["answer"]
    assert len(result["trace"]) == 1
    assert result["trace"][0]["tool"] == "list_workflows"
    assert "workflows" in result["trace"][0]["result"]  # 工具结果已回填


def test_react_agent_knowledge_tool_collects_sources():
    """query_knowledge 工具调用后，sources 从工具结果中收集。"""
    script = [
        _tool_call("query_knowledge", {"query": "请假流程", "top_k": 3}, "call-2"),
        AIMessage(content="根据知识库，请假需提前一天提交申请。"),
    ]
    result = run_react_agent(
        "请假流程是什么", session_id="t-src", kb_id="default", llm=ScriptedToolLLM(script)
    )
    assert result["mode"] == "llm"
    assert result["trace"][0]["tool"] == "query_knowledge"
    assert isinstance(result["sources"], list)
    assert "提前一天" in result["answer"]


def test_react_agent_multi_tool_combo():
    """一次推理可发起多个工具调用（list + knowledge 组合）。"""
    script = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "list_workflows", "args": {}, "id": "call-3a", "type": "tool_call"},
                {"name": "query_knowledge", "args": {"query": "报销流程"}, "id": "call-3b", "type": "tool_call"},
            ],
        ),
        AIMessage(content="为你列出流程并查询了报销说明。"),
    ]
    result = run_react_agent(
        "有哪些流程？报销怎么走", session_id="t-combo", kb_id="default", llm=ScriptedToolLLM(script)
    )
    assert result["mode"] == "llm"
    assert [t["tool"] for t in result["trace"]] == ["list_workflows", "query_knowledge"]
    assert len(result["trace"]) == 2


def test_react_agent_max_iterations_guard():
    """LLM 一直调用工具时，到达 max_iterations 强制结束并给出兜底回答。

    图流：agent → tools → agent → ... → finish；max_iterations 限制 LLM 推理轮数，
    因此工具执行轮数最多为 max_iterations - 1。
    """
    script = [_tool_call("list_workflows", {}, f"call-{i}") for i in range(10)]
    result = run_react_agent(
        "反复调用工具",
        session_id="t-max",
        kb_id="default",
        llm=ScriptedToolLLM(script),
        max_iterations=3,
    )
    assert result["mode"] == "llm"
    assert len(result["trace"]) == 2  # 只执行了 2 轮工具（第 3 轮推理强制结束）
    assert result["answer"]  # 结束节点兜底回答非空


def test_react_agent_build_tools_binds_kb():
    """build_tools 返回 3 个工具，名称与注册表一致。"""
    tools = build_tools(kb_id="default")
    names = {t.name for t in tools}
    assert names == {"query_knowledge", "launch_workflow", "list_workflows"}
    for t in tools:
        assert t.description  # 工具描述必须非空（LLM 依赖描述选工具）
