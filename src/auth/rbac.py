"""知识库访问权限（RBAC + 部门维度）。

访问规则（用户 → 可访问的知识库）：
1. admin 角色 → 可访问所有知识库
2. user 角色 → 可访问「本部门被授权」的知识库（kb.departments 含 user.department，或 kb.departments 为空=公开）
3. 用户额外授权 extra_kbs → 追加到个人可访问列表（方便调部门/临时授权）

调部门只需更新 user.department，即可切换默认可访问的知识库。
"""
from __future__ import annotations

from src.kb.manager import get_kb_manager


def user_accessible_kb_ids(user: dict | None) -> list[str]:
    """返回用户可访问的知识库 ID 列表。匿名或未启用认证时返回全部。"""
    if user is None:
        return [kb["id"] for kb in get_kb_manager().list()]

    # admin 全部可见
    if user.get("role") == "admin":
        return [kb["id"] for kb in get_kb_manager().list()]

    department = user.get("department", "")
    extra = set(user.get("extra_kbs") or [])
    result: list[str] = []
    for kb in get_kb_manager().list():
        kb_id = kb["id"]
        # 库未限定部门（空=公开）或包含本部门 → 可访问
        if not kb.get("departments") or department in kb["departments"]:
            result.append(kb_id)
            continue
        # 额外授权
        if kb_id in extra:
            result.append(kb_id)
    return result


def can_access_kb(user: dict | None, kb_id: str) -> bool:
    """判断用户是否有权访问指定知识库。"""
    if user is None:
        return True
    if user.get("role") == "admin":
        return True
    # 知识库存在性 + 权限
    kb = get_kb_manager().get(kb_id)
    if kb is None:
        return False
    if not kb.get("departments"):  # 公开库
        return True
    department = user.get("department", "")
    if department in kb["departments"]:
        return True
    if kb_id in (user.get("extra_kbs") or []):
        return True
    return False
