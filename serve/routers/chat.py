"""用户对话历史 API 路由。

所有接口均需要登录（auth_enabled=true 时），按用户隔离数据。

会话管理：
    GET    /api/chat/sessions                # 会话列表
    POST   /api/chat/sessions                # 新建会话
    GET    /api/chat/sessions/{id}/messages  # 会话消息记录
    PATCH  /api/chat/sessions/{id}           # 重命名会话
    DELETE /api/chat/sessions/{id}           # 删除会话
    GET    /api/chat/sessions/{id}/followups # 追问记录
    GET    /api/chat/search?q=keyword        # 跨会话搜索
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.auth.deps import get_current_user
from src.memory.chat_history import get_chat_history_store

router = APIRouter(prefix="/api/chat", tags=["对话历史"])


def _username(user: dict) -> str:
    username = user.get("username", "") if user else ""
    if not username:
        raise HTTPException(status_code=401, detail="请先登录")
    return username


# ===== 会话管理 =====


@router.get("/sessions", summary="会话列表")
def list_sessions(user: dict = Depends(get_current_user)) -> list[dict]:
    return get_chat_history_store().list_sessions(_username(user))


class CreateSessionReq(BaseModel):
    title: str = ""


@router.post("/sessions", summary="新建会话")
def create_session(
    body: CreateSessionReq, user: dict = Depends(get_current_user)
) -> dict:
    return get_chat_history_store().create_session(_username(user), body.title)


@router.get("/sessions/{session_id}/messages", summary="会话消息记录")
def get_messages(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
) -> list[dict]:
    return get_chat_history_store().get_messages(
        _username(user), session_id, limit=limit, offset=offset
    )


class RenameSessionReq(BaseModel):
    title: str


@router.patch("/sessions/{session_id}", summary="重命名会话")
def rename_session(
    session_id: str,
    body: RenameSessionReq,
    user: dict = Depends(get_current_user),
) -> dict:
    result = get_chat_history_store().rename_session(
        _username(user), session_id, body.title
    )
    if not result:
        raise HTTPException(status_code=404, detail="会话不存在")
    return result


@router.delete("/sessions/{session_id}", summary="删除会话")
def delete_session(
    session_id: str, user: dict = Depends(get_current_user)
) -> dict:
    get_chat_history_store().delete_session(_username(user), session_id)
    return {"ok": True}


@router.get("/sessions/{session_id}/followups", summary="追问记录")
def get_followups(
    session_id: str, user: dict = Depends(get_current_user)
) -> list[dict]:
    return get_chat_history_store().get_followups(_username(user), session_id)


@router.get("/search", summary="跨会话搜索历史消息")
def search_history(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
) -> list[dict]:
    return get_chat_history_store().search_messages(_username(user), q, limit=limit)
