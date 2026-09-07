"""多知识库接口：创建 / 列表 / 详情 / 删除 / 部门授权。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth.deps import get_current_user, require_role
from src.auth.rbac import user_accessible_kb_ids
from src.kb.manager import get_kb_manager

router = APIRouter(prefix="/api/kb", tags=["知识库"])


class KBCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    departments: list[str] = []


class KBDepartments(BaseModel):
    departments: list[str] = []


@router.get("/list", summary="知识库列表（按用户权限过滤）")
def list_kb(user: dict | None = Depends(get_current_user)) -> dict:
    accessible = user_accessible_kb_ids(user)
    all_kbs = get_kb_manager().list()
    visible = [kb for kb in all_kbs if kb["id"] in accessible]
    return {"ok": True, "total": len(visible), "knowledge_bases": visible}


@router.post("/create", summary="创建知识库（仅 admin）")
def create_kb(
    body: KBCreate,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    try:
        kb = get_kb_manager().create(
            body.id, body.name, body.description, departments=body.departments
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "knowledge_base": kb}


@router.post("/{kb_id}/departments", summary="设置知识库可访问部门（仅 admin）")
def set_departments(
    kb_id: str,
    body: KBDepartments,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    ok = get_kb_manager().set_departments(kb_id, body.departments)
    if not ok:
        raise HTTPException(status_code=404, detail=f"知识库不存在：{kb_id}")
    return {"ok": True, "kb_id": kb_id, "departments": body.departments}


@router.delete("/{kb_id}", summary="删除知识库（仅 admin）")
def delete_kb(
    kb_id: str,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    ok = get_kb_manager().delete(kb_id)
    if not ok:
        raise HTTPException(status_code=404, detail="知识库不存在或为默认库，无法删除")
    return {"ok": True, "kb_id": kb_id}
