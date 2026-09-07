"""认证接口：注册 / 登录 / 当前用户 / 用户列表 / 用户部门调整。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth.deps import get_current_user, get_optional_user, require_role
from src.auth.jwt import create_token
from src.auth.users import get_user_store

router = APIRouter(prefix="/api/auth", tags=["认证"])


class RegisterRequest(BaseModel):
    username: str
    password: str
    department: str = ""
    role: str = "user"  # 仅当调用者本身是已登录 admin 时生效（管理端建号），否则强制 user
    extra_kbs: list[str] = []  # 同上，仅 admin 调用时生效


class LoginRequest(BaseModel):
    username: str
    password: str


class UserUpdateRequest(BaseModel):
    department: str | None = None
    role: str | None = None
    extra_kbs: list[str] | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.post("/register", summary="注册用户")
def register(
    body: RegisterRequest,
    current: dict | None = Depends(get_optional_user),
) -> dict:
    # 安全：公开注册一律 user 角色，防止越权注册 admin；
    # 仅当调用者本身是已认证 admin 时，才允许按请求指定 role / extra_kbs（管理端建号场景）。
    # 提权也可由管理员事后通过 /{username}/update 完成。
    is_admin_caller = bool(current and current.get("role") == "admin")
    try:
        user = get_user_store().register(
            body.username,
            body.password,
            role=body.role if is_admin_caller else "user",
            department=body.department,
            extra_kbs=body.extra_kbs if is_admin_caller else [],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = create_token({"sub": user["username"], "role": user["role"]})
    return {"ok": True, "token": token, "user": user}


@router.post("/login", summary="用户登录")
def login(body: LoginRequest) -> dict:
    user = get_user_store().verify(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_token({"sub": user["username"], "role": user["role"]})
    return {"ok": True, "token": token, "user": user}


@router.post("/{username}/update", summary="更新用户（调部门/授权，仅 admin）")
def update_user(
    username: str,
    body: UserUpdateRequest,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    user = get_user_store().update(
        username,
        department=body.department,
        role=body.role,
        extra_kbs=body.extra_kbs,
    )
    if not user:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    return {"ok": True, "user": user}


@router.post("/{username}/reset-password", summary="重置用户密码（仅 admin）")
def reset_password(
    username: str,
    body: ResetPasswordRequest,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    try:
        user = get_user_store().reset_password(username, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not user:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    return {"ok": True, "user": user}


@router.get("/me", summary="当前用户")
def me(user: dict | None = Depends(get_current_user)) -> dict:
    if not settings_ready():
        return {"authenticated": False, "user": None}
    return {"authenticated": True, "user": user}


@router.get("/users", summary="用户列表（仅 admin）")
def list_users(_: dict | None = Depends(require_role("admin"))) -> dict:
    return {"ok": True, "users": get_user_store().list()}


def settings_ready() -> bool:
    from config.settings import settings

    return bool(settings.auth_enabled)
