"""验证会话记忆隔离修复是否生效。

测试场景：
1. 访客 A 创建会话 → 访客 B 用相同 session_id 能否读到 A 的历史
2. 登录用户 A 创建会话 → 登录用户 B 用相同 session_id 能否读到 A 的历史
3. 登录用户 A 创建会话 → 访客用相同 session_id 能否读到 A 的历史
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve.routers.qa import _bind_session


def test_anon_isolation():
    """场景1：两个访客用相同 session_id，记忆 key 应相同（anon 前缀相同）。

    修复前：anon 用户共享 session_id，记忆互通。
    修复后：访客模式下统一为 anon:session_id 前缀。
    注：访客间无法区分身份，这是设计限制（访客无身份），但至少隔离了登录用户。
    """
    anon_a = None  # 访客 A（未登录）
    anon_b = None  # 访客 B（未登录）

    key_a = _bind_session("web-123", anon_a)
    key_b = _bind_session("web-123", anon_b)

    print(f"[场景1] 访客 A 的 session_key: {key_a}")
    print(f"[场景1] 访客 B 的 session_key: {key_b}")
    print(f"[场景1] 访客间 key 相同: {key_a == key_b}")
    print(f"[场景1] 访客 key 带 anon 前缀: {key_a.startswith('anon:')}")
    print()

    # 访客间 key 相同是预期行为（访客无身份区分），但必须带 anon 前缀与登录用户隔离
    assert key_a.startswith("anon:"), "访客 key 必须带 anon: 前缀"
    assert key_b.startswith("anon:"), "访客 key 必须带 anon: 前缀"
    print("[场景1] PASS: 访客模式带 anon 前缀，与登录用户隔离\n")


def test_logged_in_isolation():
    """场景2：两个登录用户用相同 session_id，记忆 key 应不同。"""
    user_a = {"username": "alice", "role": "user"}
    user_b = {"username": "bob", "role": "user"}

    key_a = _bind_session("web-123", user_a)
    key_b = _bind_session("web-123", user_b)

    print(f"[场景2] 用户 alice 的 session_key: {key_a}")
    print(f"[场景2] 用户 bob   的 session_key: {key_b}")
    print(f"[场景2] 两者不同: {key_a != key_b}")
    print()

    assert key_a != key_b, "不同用户的 session_key 必须不同"
    assert "alice" in key_a, "alice 的 key 必须包含用户名"
    assert "bob" in key_b, "bob 的 key 必须包含用户名"
    print("[场景2] PASS: 登录用户间完全隔离\n")


def test_anon_vs_logged_in():
    """场景3：登录用户 A 创建会话 → 访客用相同 session_id 能否读到 A 的历史。"""
    user_a = {"username": "alice", "role": "user"}
    anon = None

    key_a = _bind_session("web-123", user_a)
    key_anon = _bind_session("web-123", anon)

    print(f"[场景3] 用户 alice 的 session_key: {key_a}")
    print(f"[场景3] 访客      的 session_key: {key_anon}")
    print(f"[场景3] 两者不同: {key_a != key_anon}")
    print()

    assert key_a != key_anon, "登录用户与访客的 session_key 必须不同"
    assert key_a.startswith("alice:"), "登录用户 key 必须带用户名前缀"
    assert key_anon.startswith("anon:"), "访客 key 必须带 anon 前缀"
    print("[场景3] PASS: 登录用户与访客完全隔离\n")


def test_admin_vs_user():
    """场景4：admin 和普通用户用相同 session_id，记忆 key 应不同。"""
    admin = {"username": "root", "role": "admin"}
    user = {"username": "alice", "role": "user"}

    key_admin = _bind_session("web-123", admin)
    key_user = _bind_session("web-123", user)

    print(f"[场景4] admin 的 session_key: {key_admin}")
    print(f"[场景4] user  的 session_key: {key_user}")
    print(f"[场景4] 两者不同: {key_admin != key_user}")
    print()

    assert key_admin != key_user, "不同用户的 session_key 必须不同"
    print("[场景4] PASS: admin 与普通用户完全隔离\n")


def test_same_user_different_sessions():
    """场景5：同一用户不同 session_id，记忆 key 应不同。"""
    user = {"username": "alice", "role": "user"}

    key1 = _bind_session("web-111", user)
    key2 = _bind_session("web-222", user)

    print(f"[场景5] alice 的 session1 key: {key1}")
    print(f"[场景5] alice 的 session2 key: {key2}")
    print(f"[场景5] 两者不同: {key1 != key2}")
    print()

    assert key1 != key2, "同一用户不同 session 的 key 必须不同"
    print("[场景5] PASS: 同一用户的不同会话正常区分\n")


if __name__ == "__main__":
    print("=" * 60)
    print("  会话记忆隔离验证")
    print("=" * 60)
    print()

    test_anon_isolation()
    test_logged_in_isolation()
    test_anon_vs_logged_in()
    test_admin_vs_user()
    test_same_user_different_sessions()

    print("=" * 60)
    print("  全部验证通过")
    print("=" * 60)
