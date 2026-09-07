"""FastAPI 认证依赖注入。

- get_current_user：从 Authorization: Bearer <token> 解析当前用户
- require_role：RBAC 角色校验（admin / user）
认证可通过 settings.auth_enabled 关闭（演示模式匿名访问）。
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status

from config.settings import settings
from src.auth.jwt import decode_token


def get_token_from_header(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _build_user(payload: dict) -> dict:
    """由 JWT payload 构建用户上下文，并回查 UserStore 补齐 department/extra_kbs。

    JWT 中仅存 username/role（避免 token 过大、过期后仍持旧授权）。
    部门与额外授权从用户存储**实时回查**，保证调部门/改授权后立即生效。
    用户已被删除时回退到仅含 username/role 的最小结构（不含部门权限）。
    """
    username = payload.get("sub", "")
    role = payload.get("role", "user")
    user = {"username": username, "role": role, "department": "", "extra_kbs": []}
    if username:
        try:
            from src.auth.users import get_user_store

            stored = get_user_store().get(username)
            if stored:
                user["role"] = stored.get("role", role)
                user["department"] = stored.get("department", "")
                user["extra_kbs"] = stored.get("extra_kbs", [])
        except Exception:  # noqa: BLE001
            pass
    return user


def get_current_user(request: Request) -> dict | None:
    """返回当前用户（含 username/role/department/extra_kbs）。auth_enabled 关闭时返回 None（匿名）。

    需要强制登录时使用：无 token 或 token 非法时抛 401。
    """
    if not settings.auth_enabled:
        return None
    token = get_token_from_header(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少认证令牌"
        )
    payload = decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期"
        )
    return _build_user(payload)


def get_optional_user(request: Request) -> dict | None:
    """可选认证：有有效 token 则返回用户，否则返回 None（匿名），不抛 401。

    用于问答等「匿名可访问、登录后按权限收窄」的接口。
    """
    if not settings.auth_enabled:
        return None
    token = get_token_from_header(request)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    return _build_user(payload)


def require_role(*roles: str) -> Any:
    """生成 RBAC 依赖：要求当前用户属于指定角色之一。"""

    def dependency(user: dict | None = Depends(get_current_user)) -> dict | None:
        if not settings.auth_enabled:
            return user
        if user is None or user.get("role") not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要角色权限：{', '.join(roles)}",
            )
        return user

    return dependency


def require_kb_access(kb_id: str, user: dict | None = Depends(get_current_user)) -> str:
    """校验当前用户对指定知识库的访问权，返回 kb_id。

    未启用认证时放行（匿名演示）。无权限抛 403。
    """
    if not settings.auth_enabled:
        return kb_id
    from src.auth.rbac import can_access_kb

    if not can_access_kb(user, kb_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"无权访问知识库：{kb_id}",
        )
    return kb_id
