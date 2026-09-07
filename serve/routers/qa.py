"""问答接口路由（2.0：支持知识库隔离 + 权限校验 + 会话隔离 + 对话历史保存）。"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config.settings import settings
from src.agent.agent import run_agent
from src.agent.react_agent import run_react_agent
from src.agent.tools import get_workflow_doc
from src.auth.deps import get_current_user, get_optional_user, require_role
from src.auth.rbac import can_access_kb
from src.chains.conversational_qa import answer_with_history, clear_session
from src.chains.retrieval_qa import ask as ask_single
from src.prompts.templates import QA_SYSTEM
from src.utils.helpers import format_sources

router = APIRouter(prefix="/api/qa", tags=["问答"])


def _bind_session(session_id: str, user: dict | None) -> str:
    """将 session_id 与用户身份绑定，防止跨用户读取会话记忆。

    - 已登录：session_key = "username:session_id"，用户 A 无法访问用户 B 的会话
    - 访客模式：session_key = "anon:session_id"（统一前缀，至少隔离命名空间）
    """
    if user and user.get("username"):
        return f"{user['username']}:{session_id}"
    return f"anon:{session_id}"


def _require_kb_access(kb_id: str, user: dict | None) -> None:
    """按权限校验知识库访问（问答接口为匿名可访问，登录后按权限收窄）。

    - 未启用认证：放行（匿名演示）。
    - 已登录用户：无权限访问该库时抛 403。
    直接调用 ``can_access_kb``，避免依赖 ``require_kb_access`` 的
    ``Depends(get_current_user)`` 强制登录语义与 ``get_optional_user`` 冲突。
    """
    if not settings.auth_enabled:
        return
    if not can_access_kb(user, kb_id):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail=f"无权访问知识库：{kb_id}")


def _save_chat(user: dict | None, session_id: str, question: str,
               answer: str, sources: list, is_followup: bool = False) -> None:
    """保存对话到用户历史记录（已登录才保存，访客跳过）。"""
    if not user or not user.get("username"):
        return
    try:
        from src.memory.chat_history import get_chat_history_store

        store = get_chat_history_store()
        username = user["username"]
        if not store.get_session(username, session_id):
            # 用前端传入的 session_id 创建会话，保证消息目录与 sessions.json 登记一致，
            # 否则 create_session() 生成的随机 id 会让消息写进另一个目录、历史记录丢失。
            store.create_session(username, session_id=session_id)
        store.add_message(username, session_id, "user", question, is_followup=is_followup)
        store.add_message(
            username, session_id, "assistant", answer,
            sources=[{"source": s.get("source", "")} for s in (sources or [])],
        )
    except Exception:
        pass


class Question(BaseModel):
    question: str = Field(..., min_length=1, description="用户问题")
    session_id: str = Field(default="default", description="会话 ID")
    top_k: int = Field(default=5, ge=1, le=20)
    kb_id: str = Field(default="default", description="知识库 ID")


class QAResponse(BaseModel):
    answer: str
    sources: list = []


@router.post("/ask", response_model=QAResponse, summary="单次问答")
def ask_qa(
    body: Question,
    user: dict | None = Depends(get_optional_user),
) -> QAResponse:
    """基于知识库的语义检索问答（按权限限定知识库）。"""
    _require_kb_access(body.kb_id, user)
    result = ask_single(body.question, top_k=body.top_k, kb_id=body.kb_id)
    return QAResponse(answer=result["answer"], sources=format_sources(result["sources"]))


@router.post("/chat", response_model=QAResponse, summary="多轮对话问答")
def chat(
    body: Question,
    user: dict | None = Depends(get_optional_user),
) -> QAResponse:
    """带会话记忆的多轮问答（按权限限定知识库）。"""
    _require_kb_access(body.kb_id, user)
    sid = _bind_session(body.session_id, user)
    result = answer_with_history(sid, body.question, top_k=body.top_k, kb_id=body.kb_id)
    _save_chat(user, body.session_id, body.question, result["answer"], result["sources"])
    return QAResponse(answer=result["answer"], sources=result["sources"])


@router.post("/agent", response_model=dict, summary="智能体问答（工具调用）")
def agent_qa(
    body: Question,
    user: dict | None = Depends(get_optional_user),
) -> dict:
    """通过 Agent 处理：支持知识问答与流程办理等工具调用（按权限限定知识库）。"""
    _require_kb_access(body.kb_id, user)
    sid = _bind_session(body.session_id, user)
    result = run_agent(body.question, session_id=sid, kb_id=body.kb_id)
    # 统一沉淀非 query 意图的对话历史；query 意图由前端走 /chat/stream 保存，避免重复
    if result.get("intent") != "query":
        _save_chat(
            user, body.session_id, body.question,
            str(result.get("answer", "")), result.get("sources") or []
        )
    return result


@router.post("/agent/react", response_model=dict, summary="智能体问答（LLM 自主工具调用）")
def agent_react_qa(
    body: Question,
    user: dict | None = Depends(get_optional_user),
) -> dict:
    """ReAct Agent：由 LLM 自主决策调用哪个工具（可多轮组合），支持知识问答与流程办理。

    与 /api/qa/agent 的区别：/agent 用规则意图分类路由到固定节点；
    /agent/react 由 LLM 每轮自主选择「直接回答 / 调用工具」，未配置真实 LLM 时自动回退规则 Agent。
    """
    _require_kb_access(body.kb_id, user)
    sid = _bind_session(body.session_id, user)
    result = run_react_agent(body.question, session_id=sid, kb_id=body.kb_id)
    _save_chat(user, body.session_id, body.question, str(result.get("answer", "")), [])
    return result


class WorkflowDraftRequest(BaseModel):
    text: str = Field(description="用户对办理需求的自然语言描述")
    workflow_key: str | None = Field(default=None, description="流程标识，缺省时从文本自动识别")
    session_id: str = Field(default="default", description="会话 ID")


class WorkflowSubmitRequest(BaseModel):
    workflow_key: str
    params: dict = Field(default_factory=dict)
    session_id: str = "default"


@router.post("/workflow/draft", summary="流程申请草稿（结构化提取，供前端确认表单）")
def workflow_draft(
    body: WorkflowDraftRequest,
    user: dict | None = Depends(get_optional_user),
) -> dict:
    """固定流程走 LangChain 结构化链：LLM 提取参数 → 表单草稿 → 前端确认后提交。"""
    from src.agent.workflow_chain import build_draft
    from src.models.llm import get_llm

    llm = get_llm()
    # DemoLLM 无结构化输出，build_draft 内部自动回退规则提取
    draft = build_draft(body.text, body.workflow_key, llm=llm)
    return {"ok": True, "draft": draft, "session_id": _bind_session(body.session_id, user)}


@router.post("/workflow/submit", summary="提交流程申请（确认表单后调用）")
def workflow_submit(
    body: WorkflowSubmitRequest,
    user: dict | None = Depends(get_optional_user),
) -> dict:
    """校验必填项后定向提交，返回工单信息。"""
    from src.agent.workflow_chain import submit_workflow

    result = submit_workflow(body.workflow_key, body.params)
    if not result.get("launched"):
        raise HTTPException(status_code=400, detail=result.get("error", "提交失败"))
    # 工单结果沉淀为对话历史
    _save_chat(
        user, body.session_id,
        f"提交{result.get('workflow_name', body.workflow_key)}申请",
        f"已发起【{result.get('workflow_name', body.workflow_key)}】，工单号：{result.get('ticket_id', '')}",
        [],
    )
    return {"ok": True, "action": result}


def _emit(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _iter_sse(answer: str, sources: list) -> AsyncIterator[str]:
    """回退：将完整回答拆成逐字 token 的 SSE 流（同步 LLM 兜底）。"""
    yield _emit({"type": "sources", "sources": sources})

    tokens: list[str] = []
    for token in answer.split():
        if token and token[0].isascii() and token.isalnum():
            tokens.append(token)
        else:
            tokens.extend(list(token))
    if len(tokens) == 1 and answer and len(answer) > 1:
        tokens = list(answer)

    for t in tokens:
        yield _emit({"type": "token", "content": t})
        time.sleep(0.02)

    yield _emit({"type": "done"})


def _iter_real_stream(session_id: str, question: str, top_k: int, kb_id: str = "default") -> AsyncIterator[str]:
    """真 LLM 流式：检索 + 生成，逐 token 推送（2.0）。

    v2.4：①先推 status 状态事件，消除思考模型下首字前的"卡住"感；
         ②指代消解+查询改写合并为单次 LLM 调用（prepare_query）。
    """
    from src.core import get_container
    from src.models.llm import get_llm
    from src.retrieval.retriever import search_with_context

    container = get_container()
    memory = container.get_session_memory()
    history = memory.get_history(session_id)
    llm = get_llm()

    from src.chains.conversational_qa import _recall_long_memory, prepare_query
    from src.chains.context_compressor import compress_context

    # ① 查询准备（有历史才需要消解，合并为单次调用）
    if history and settings.condense_history:
        yield _emit({"type": "status", "message": "理解问题，优化检索词…"})
        q = prepare_query(question, history, llm)
    else:
        q = question

    # ② 检索 + 上下文压缩
    yield _emit({"type": "status", "message": "检索知识库…"})
    try:
        long_memory = _recall_long_memory(session_id, q)
        _, docs = search_with_context(q, top_k=top_k, kb_id=kb_id)
        docs = compress_context(docs, query=question)
        context = "\n\n".join(
            f"[{i + 1}] {getattr(d, 'page_content', str(d))}"
            for i, d in enumerate(docs)
        )
    except Exception as e:  # noqa: BLE001
        yield _emit({"type": "error", "message": f"检索失败：{e}"})
        yield _emit({"type": "done"})
        return ""

    history_text = "\n".join(
        f"{m.type}: {m.content}" for m in history[-settings.memory_k * 2 :]
    )
    system = QA_SYSTEM.format(context=context)
    if long_memory:
        system += "\n\n【长期记忆】\n" + "\n".join(f"- {m}" for m in long_memory)
    if history_text:
        system += "\n\n【历史对话】\n" + history_text

    sources = [
        {
            "source": getattr(d, "metadata", {}).get("source", ""),
            "doc_id": getattr(d, "metadata", {}).get("doc_id", ""),
            "content": getattr(d, "page_content", ""),
        }
        for d in docs
    ]
    yield _emit({"type": "sources", "sources": sources})

    # ③ 生成（真流式逐 token 推送）
    yield _emit({"type": "status", "message": "生成回答…"})
    answer_text = ""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = [SystemMessage(content=system), HumanMessage(content=question)]
        if hasattr(llm, "stream"):
            chunks: list[str] = []
            for chunk in llm.stream(prompt):
                content = getattr(chunk, "content", "")
                if content:
                    chunks.append(content)
                    yield _emit({"type": "token", "content": content})
            answer_text = "".join(chunks)
        else:  # DemoLLM 无 stream，回退整段
            answer_text = _extract_plain(llm.invoke(prompt))
            yield _emit({"type": "token", "content": answer_text})
    except (TypeError, AttributeError):
        answer_text = _extract_plain(llm.invoke(system + "\n\n用户问题：" + question))
        yield _emit({"type": "token", "content": answer_text})
    except Exception as e:  # noqa: BLE001
        # LLM 调用失败（Key 不匹配/额度用尽/网络）：显式上报，避免前端静默显示"无法回答"
        yield _emit({"type": "error", "message": f"模型调用失败：{e}"})
        yield _emit({"type": "done"})
        return ""

    if not answer_text.strip():
        yield _emit({"type": "error", "message": "模型返回了空回答，请检查模型配置（Key 与服务商是否匹配、额度是否充足）"})
        yield _emit({"type": "done"})
        return ""

    memory.add_turn(session_id, question, answer_text)
    # ④ 推荐追问：基于本次问答生成 3 个可能的问题（失败静默，不影响主流程）
    yield _emit({"type": "followups", "items": _generate_followups(llm, question, answer_text)})
    yield _emit({"type": "done"})
    return answer_text


def _generate_followups(llm: Any, question: str, answer: str) -> list[str]:
    """生成推荐追问（猜你想问）。LLM 失败或演示模式时返回通用兜底。"""
    from src.prompts.templates import DEFAULT_FOLLOWUPS, FOLLOWUP_SYSTEM

    if not answer or len(answer) < 20:
        return list(DEFAULT_FOLLOWUPS)
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = [
            SystemMessage(content=FOLLOWUP_SYSTEM),
            HumanMessage(content=f"用户问题：{question}\n\n回答摘要：{answer[:500]}"),
        ]
        resp = llm.invoke(prompt)
        text = getattr(resp, "content", "") or ""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        items = json.loads(text)
        if isinstance(items, list):
            cleaned = [str(x).strip() for x in items if str(x).strip()][:3]
            if cleaned:
                return cleaned
    except Exception:  # noqa: BLE001
        pass
    return list(DEFAULT_FOLLOWUPS)


def _extract_plain(answer) -> str:
    if isinstance(answer, str):
        return answer
    content = getattr(answer, "content", answer)
    if isinstance(content, list):
        content = " ".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    return str(content) if content is not None else ""


@router.post("/chat/stream", summary="多轮对话流式输出（SSE）")
def chat_stream(
    body: Question,
    user: dict | None = Depends(get_optional_user),
) -> StreamingResponse:
    """基于会话记忆的多轮问答，返回 SSE 流式结果（2.0 真流式，按权限限定知识库）。

    事件格式（每行 `data: {json}`）：
    - {"type": "sources", "sources": [...]}  参考资料
    - {"type": "token", "content": "..."}    增量回答文本
    - {"type": "done"}                       结束
    """
    _require_kb_access(body.kb_id, user)
    sid = _bind_session(body.session_id, user)

    async def _wrapped():
        answer_text = ""
        sources_data: list = []
        # _iter_real_stream 是同步生成器（内部用同步 llm.stream），用 for 而非 async for
        for chunk in _iter_real_stream(sid, body.question, body.top_k, body.kb_id):
            try:
                event = json.loads(chunk.strip().removeprefix("data: "))
                if event.get("type") == "sources":
                    sources_data = event.get("sources", [])
                elif event.get("type") == "token":
                    answer_text += event.get("content", "")
            except Exception:
                pass
            yield chunk
        _save_chat(user, body.session_id, body.question, answer_text, sources_data)

    return StreamingResponse(
        _wrapped(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/workflow/{key}/doc", summary="查看流程对应的制度原文")
def workflow_doc(
    key: str,
    kb_id: str | None = Query(None),
    user: dict | None = Depends(get_optional_user),
) -> dict:
    """返回某个流程对应的制度/规定原文（从对应部门知识库检索，匿名可访问）。

    kb_id 缺省时使用流程定义的目标库（如请假→hr、报销→finance）。
    """
    return get_workflow_doc(key, kb_id=kb_id)


@router.post("/clear", summary="清空会话记忆")
def clear(body: dict, user: dict | None = Depends(get_optional_user)) -> dict:
    session_id = body.get("session_id", "default")
    sid = _bind_session(session_id, user)
    clear_session(sid)
    return {"ok": True, "session_id": session_id}


# ---- v1.5：用户反馈接口 ----

class Feedback(BaseModel):
    question: str = Field(..., description="用户问题")
    answer: str = Field(..., description="系统回答")
    score: int = Field(..., ge=1, le=5, description="评分 1-5（5 最满意）")
    kb_id: str = Field(default="default", description="知识库 ID")
    comment: str = Field(default="", description="文字反馈")
    sources: list = Field(default=[], description="引用来源")


@router.post("/feedback", summary="提交回答评分反馈")
def submit_feedback(body: Feedback) -> dict:
    """用户对回答评分，低分（≤2）自动标记为待优化。

    反馈持久化到 data/feedback/feedback.jsonl，可通过 /api/qa/feedback/list 查看。
    """
    from src.eval.feedback import get_feedback_store

    store = get_feedback_store()
    entry = store.record(
        question=body.question,
        answer=body.answer,
        score=body.score,
        sources=body.sources,
        kb_id=body.kb_id,
        comment=body.comment,
    )
    return {"ok": True, "id": entry["id"], "status": entry["status"]}


@router.get("/feedback/list", summary="查询用户反馈（仅 admin）")
def list_feedback(
    status: str | None = Query(None, description="按状态过滤：pending / resolved"),
    min_score: int | None = Query(None, description="最低分"),
    max_score: int | None = Query(None, description="最高分"),
    limit: int = Query(100, ge=1, le=1000),
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    """查询用户反馈记录（需 admin 角色）。"""
    from src.eval.feedback import get_feedback_store

    store = get_feedback_store()
    records = store.list_feedback(
        status=status, min_score=min_score, max_score=max_score, limit=limit
    )
    stats = store.stats()
    return {"records": records, "stats": stats}


@router.post("/feedback/{feedback_id}/resolve", summary="标记反馈已处理（仅 admin）")
def resolve_feedback(
    feedback_id: str,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    """将低分反馈标记为已处理（需 admin 角色）。"""
    from src.eval.feedback import get_feedback_store

    store = get_feedback_store()
    ok = store.update_status(feedback_id, "resolved")
    return {"ok": ok, "id": feedback_id}


# ---- v1.5：系统统计接口 ----

@router.get("/stats", summary="系统运行统计（需登录）")
def system_stats(_: dict | None = Depends(get_current_user)) -> dict:
    """返回缓存命中率、Token 用量、反馈统计等运维指标（认证开启时需登录）。"""
    from src.retrieval.cache import get_query_cache
    from src.core.token_tracker import get_token_tracker
    from src.eval.feedback import get_feedback_store

    return {
        "cache": get_query_cache().stats(),
        "tokens": get_token_tracker().stats(),
        "feedback": get_feedback_store().stats(),
    }
