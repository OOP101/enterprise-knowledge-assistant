"""2.0 新特性测试：认证 / 知识库 / Eval 指标。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth.jwt import create_token, decode_token
from src.auth.users import UserStore
from src.eval.metrics import f1, hit_rate, precision
from src.kb.manager import KnowledgeBaseManager


def test_jwt_roundtrip():
    token = create_token({"sub": "alice", "role": "admin"})
    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == "alice"
    assert payload["role"] == "admin"


def test_jwt_tampered_rejected():
    token = create_token({"sub": "alice", "role": "user"})
    tampered = token[:-1] + ("B" if token[-1] != "B" else "C")
    assert decode_token(tampered) is None


def test_user_store_register_verify(tmp_path):
    store = UserStore(path=tmp_path / "users.json")
    store.register("bob", "secret123", role="admin")
    user = store.verify("bob", "secret123")
    assert user is not None and user["role"] == "admin"
    assert store.verify("bob", "wrong") is None
    assert store.get("bob")["username"] == "bob"


def test_kb_manager_crud(tmp_path):
    mgr = KnowledgeBaseManager(path=tmp_path / "kbs.json")
    kb = mgr.create("hr", "HR 知识库", "人事相关")
    assert kb["id"] == "hr"
    assert kb["collection"] == "enterprise_knowledge_hr"
    assert len(mgr.list()) == 1
    assert mgr.delete("hr") is True
    assert mgr.delete("hr") is False


def test_eval_metrics():
    recalled = ["a.md", "b.md", "c.md"]
    gold = ["a.md", "b.md"]
    assert hit_rate(recalled, gold) == 1.0
    assert precision(recalled, gold) == round(2 / 3, 4)
    assert f1(recalled, gold) == round(2 * (1.0 * 2 / 3) / (1.0 + 2 / 3), 4)


def test_rbac_admin_sees_all(tmp_path, monkeypatch):
    from src.auth.rbac import can_access_kb, user_accessible_kb_ids
    from src.kb.manager import KnowledgeBaseManager, get_kb_manager

    mgr = KnowledgeBaseManager(path=tmp_path / "kbs.json")
    mgr.ensure_default()  # default 库（公开）
    mgr.create("hr", "HR", departments=["hr_dept"])
    mgr.create("finance", "财务", departments=["finance_dept"])
    mgr.create("public", "公共", departments=[])

    # 打桩：让全局管理器使用临时实例
    import src.kb.manager as kb_mod

    monkeypatch.setattr(kb_mod, "_manager", mgr)

    # admin 看到全部
    admin = {"role": "admin", "department": "whatever"}
    assert set(user_accessible_kb_ids(admin)) == {"hr", "finance", "public", "default"}

    # 普通用户只看到本部门库 + 公开库
    hr_user = {"role": "user", "department": "hr_dept", "extra_kbs": []}
    accessible = user_accessible_kb_ids(hr_user)
    assert "hr" in accessible
    assert "finance" not in accessible
    assert "public" in accessible

    # 无权限访问则拒绝
    assert can_access_kb(hr_user, "hr") is True
    assert can_access_kb(hr_user, "finance") is False


def test_rbac_extra_kb_override(tmp_path, monkeypatch):
    from src.auth.rbac import can_access_kb
    from src.kb.manager import KnowledgeBaseManager
    import src.kb.manager as kb_mod

    mgr = KnowledgeBaseManager(path=tmp_path / "kbs2.json")
    mgr.create("finance", "财务", departments=["finance_dept"])
    monkeypatch.setattr(kb_mod, "_manager", mgr)

    # 通过 extra_kbs 单独授权
    user = {"role": "user", "department": "hr_dept", "extra_kbs": ["finance"]}
    assert can_access_kb(user, "finance") is True

    # 无 extra 则拒绝
    user2 = {"role": "user", "department": "hr_dept", "extra_kbs": []}
    assert can_access_kb(user2, "finance") is False
