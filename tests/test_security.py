"""安全回归测试（2026-08-27 代码评审修复项）。

覆盖：
1. /api/qa/feedback/list、/feedback/{id}/resolve 的 admin 鉴权（原为匿名可访问）
2. /api/qa/stats 登录要求
3. 密码强度校验 + scrypt 慢哈希 + 旧 SHA-256 哈希惰性迁移
4. 上传大小上限
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from config.settings import get_settings
from src.auth.jwt import create_token
from src.auth.users import UserStore

APP = None


def _client() -> TestClient:
    global APP
    if APP is None:
        from serve.main import app

        APP = app
    return TestClient(APP)


@pytest.fixture()
def auth_on(monkeypatch):
    """临时开启认证（不影响其它测试）。"""
    s = get_settings()
    monkeypatch.setattr(s, "auth_enabled", True)
    return s


def _admin_token() -> str:
    # sub 不在用户存储中时回退 JWT 内 role，便于测试
    return create_token({"sub": "sec_admin", "role": "admin"})


def _user_token() -> str:
    return create_token({"sub": "sec_user", "role": "user"})


# ---- 1. 反馈管理接口鉴权 ----

def test_feedback_list_requires_admin(auth_on):
    c = _client()
    assert c.get("/api/qa/feedback/list").status_code == 401
    r = c.get(
        "/api/qa/feedback/list",
        headers={"Authorization": f"Bearer {_user_token()}"},
    )
    assert r.status_code == 403
    r = c.get(
        "/api/qa/feedback/list",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert r.status_code == 200


def test_feedback_resolve_requires_admin(auth_on):
    c = _client()
    assert c.post("/api/qa/feedback/fake-id/resolve").status_code == 401
    r = c.post(
        "/api/qa/feedback/fake-id/resolve",
        headers={"Authorization": f"Bearer {_user_token()}"},
    )
    assert r.status_code == 403


def test_stats_requires_login(auth_on):
    c = _client()
    assert c.get("/api/qa/stats").status_code == 401
    r = c.get("/api/qa/stats", headers={"Authorization": f"Bearer {_user_token()}"})
    assert r.status_code == 200


# ---- 2. 密码强度与哈希 ----

def test_password_min_length_rejected(tmp_path):
    store = UserStore(path=tmp_path / "users.json")
    with pytest.raises(ValueError):
        store.register("bob", "abc")  # 3 位，过短
    store.register("bob", "password8")
    with pytest.raises(ValueError):
        store.reset_password("bob", "1234567")


def test_password_hash_is_scrypt(tmp_path):
    store = UserStore(path=tmp_path / "users.json")
    store.register("bob", "password8")
    raw = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))
    assert raw["bob"]["password_hash"].startswith("scrypt$")
    assert store.verify("bob", "password8") is not None
    assert store.verify("bob", "wrong-pass") is None


def test_legacy_sha256_hash_migrates(tmp_path):
    """旧格式（64 位 hex SHA-256）登录成功后应惰性升级为 scrypt。"""
    path = tmp_path / "users.json"
    salt = "47fd878eb3231004"
    legacy_hash = hashlib.sha256(f"{salt}:old-pass-123".encode("utf-8")).hexdigest()
    path.write_text(
        json.dumps(
            {
                "alice": {
                    "username": "alice",
                    "role": "user",
                    "department": "",
                    "extra_kbs": [],
                    "salt": salt,
                    "password_hash": legacy_hash,
                    "created_at": "2026-01-01T00:00:00",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = UserStore(path=path)
    assert store.verify("alice", "old-pass-123") is not None
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["alice"]["password_hash"].startswith("scrypt$")
    # 迁移后旧密码仍可登录
    assert store.verify("alice", "old-pass-123") is not None


# ---- 3. 上传大小上限 ----

def test_upload_size_limit(monkeypatch, tmp_path):
    import serve.routers.upload as upload_mod

    s = get_settings()
    monkeypatch.setattr(s, "auth_enabled", False)
    monkeypatch.setattr(upload_mod, "MAX_UPLOAD_BYTES", 10)
    c = _client()
    r = c.post(
        "/api/upload",
        files={"file": ("big.txt", b"x" * 11, "text/plain")},
        data={"kb_id": "default"},
    )
    assert r.status_code == 413


# ---- 4. P0-4：knowledge/stats 与 model/config 鉴权 ----

def test_knowledge_stats_requires_login(auth_on):
    c = _client()
    assert c.get("/api/knowledge/stats").status_code == 401
    r = c.get(
        "/api/knowledge/stats",
        headers={"Authorization": f"Bearer {_user_token()}"},
    )
    assert r.status_code == 200


def test_model_config_requires_login(auth_on):
    c = _client()
    assert c.get("/api/model/config").status_code == 401
    r = c.get(
        "/api/model/config",
        headers={"Authorization": f"Bearer {_user_token()}"},
    )
    assert r.status_code == 200


# ---- 5. P0-5：kb_id 格式校验 ----

def test_kb_id_format_validation(tmp_path):
    from src.kb.manager import KnowledgeBaseManager

    mgr = KnowledgeBaseManager(path=tmp_path / "kbs.json")
    for bad in ("bad id", "a/b", "a b", "x" * 65, ""):
        with pytest.raises(ValueError):
            mgr.create(bad, "测试库")
    kb = mgr.create("ok-id_1", "测试库")
    assert kb["id"] == "ok-id_1"


# ---- 6. P0-3：chat_history 并发下 sessions.json 不损坏 ----

def test_chat_history_sessions_survive_concurrency(tmp_path):
    from src.memory.chat_history import ChatHistoryStore

    store = ChatHistoryStore(base_dir=tmp_path / "history")
    store.create_session("carol", session_id="s1")
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(store.add_message, "carol", "s1", "user", f"消息{i}")
            for i in range(20)
        ] + [pool.submit(store.rename_session, "carol", "s1", "改名会话")]
        for f in futures:
            f.result()

    sessions_file = tmp_path / "history" / "carol" / "sessions.json"
    sessions = json.loads(sessions_file.read_text(encoding="utf-8"))
    assert len(sessions) == 1
    assert sessions[0]["message_count"] == 20
    assert sessions[0]["title"] == "改名会话"
