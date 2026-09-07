"""固定流程办理链测试（v2.4）：起草 / 规则提取 / 必填校验 / 提交。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.workflow_chain import build_draft, submit_workflow


def test_draft_regex_extract_leave():
    """规则提取：日期与请假类型应被识别。"""
    draft = build_draft("我9月3日到9月5日休年假，家里有事", llm=None)
    assert draft["workflow_key"] == "leave"
    params = {f["key"]: f["value"] for f in draft["fields"]}
    assert params["leave_type"] == "年假"
    assert params["start_date"] == "09-03"  # 缺年份的短格式（前端 date 控件会引导用户重新选择）
    assert params["end_date"] == "09-05"
    assert draft["missing"] == []  # 必填项均已提取到


def test_draft_full_date_recognized():
    draft = build_draft("2026-09-03 请年假一天", llm=None)
    params = {f["key"]: f["value"] for f in draft["fields"]}
    assert params["start_date"] == "2026-09-03"


def test_draft_overtime_hours():
    draft = build_draft("9月10日加班3小时", llm=None)
    assert draft["workflow_key"] == "overtime"
    params = {f["key"]: f["value"] for f in draft["fields"]}
    assert params["hours"] == 3.0


def test_submit_rejects_missing_required():
    result = submit_workflow("leave", {})
    assert result["launched"] is False
    assert "missing" in result


def test_submit_success_mock():
    result = submit_workflow("leave", {
        "leave_type": "年假", "start_date": "2026-09-03",
        "end_date": "2026-09-04", "reason": "家中有事",
    })
    assert result["launched"] is True
    assert result["ticket_id"]
    assert result["workflow_name"] == "请假申请"


def test_submit_unknown_workflow():
    assert submit_workflow("nope", {})["launched"] is False
