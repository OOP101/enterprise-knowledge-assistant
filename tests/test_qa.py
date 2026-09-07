"""问答链路测试（演示模式，无需 API Key）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.agent import classify_intent, run_agent
from src.chains.retrieval_qa import ask


def test_classify_intent():
    assert classify_intent("我想申请请假") == "action"
    assert classify_intent("有哪些流程") == "list"
    assert classify_intent("请假需要什么材料") == "query"


def test_agent_action():
    """v2.4：action 意图不再直接开单，而是返回结构化草稿供前端确认。"""
    result = run_agent("帮我提交一个请假申请")
    assert result["intent"] == "action"
    assert result["action"] is None
    draft = result["draft"]
    assert draft["workflow_key"] == "leave"
    assert draft["fields"]


def test_ask_demo_mode():
    """演示模式下问答不应崩溃。"""
    result = ask("什么是请假流程")
    assert "answer" in result
    assert isinstance(result["answer"], str)
