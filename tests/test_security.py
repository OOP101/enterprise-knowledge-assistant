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


# ---- 5. 用户表损坏不得丢数据（2026-09-25 OCR 审查修复项）----


def test_corrupt_users_file_does_not_wipe_data(tmp_path):
    """users.json 解析失败时：不得静默清空、必须备份原文、必须阻断写回。

    回归背景：原实现 `except Exception: self._users = {}`，紧接着任意一次
    `_save()` 就会把空表覆盖回磁盘 → 全部账号永久丢失且无任何痕迹。
    """
    path = tmp_path / "users.json"
    corrupt = '{"admin": {"role": "admin"'
    path.write_text(corrupt, encoding="utf-8")

    store = UserStore(path=path)

    # 1) 进入只读降级模式
    assert store._degraded is True
    assert store._users == {}

    # 2) 损坏文件已被备份，且备份保留原始字节
    backups = list(tmp_path.glob("users.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == corrupt

    # 3) 写回被拒绝，原始文件未被覆盖
    with pytest.raises(RuntimeError):
        store.register("bob", "Str0ng-pass-2026")
    assert path.read_text(encoding="utf-8") == corrupt


def test_valid_users_file_not_affected_by_guard(tmp_path):
    """加固不得误伤正常路径：正常文件仍可读写、不进入降级。"""
    path = tmp_path / "users.json"
    UserStore(path=path).register("bob", "Str0ng-pass-2026")

    reopened = UserStore(path=path)
    assert reopened._degraded is False
    assert reopened.verify("bob", "Str0ng-pass-2026") is not None
    assert not list(tmp_path.glob("users.json.corrupt-*"))


def test_non_object_users_file_treated_as_corrupt(tmp_path):
    """合法 JSON 但不是对象（如数组）同样视为损坏，不得放行。"""
    path = tmp_path / "users.json"
    path.write_text('["not", "a", "dict"]', encoding="utf-8")
    store = UserStore(path=path)
    assert store._degraded is True
    with pytest.raises(RuntimeError):
        store.register("bob", "Str0ng-pass-2026")


def test_weak_password_rejected(tmp_path):
    """常见弱口令（含项目自带默认值 admin123）不得被设置为密码。"""
    store = UserStore(path=tmp_path / "users.json")
    for weak in ("admin123", "12345678", "password", "changeme"):
        with pytest.raises(ValueError):
            store.register("bob", weak)
    # 强口令不受影响
    store.register("bob", "Str0ng-pass-2026")
    assert store.verify("bob", "Str0ng-pass-2026") is not None


def test_user_store_singleton_built_once_under_concurrency():
    """并发首访只构造一个 UserStore（双检锁回归）。"""
    import threading

    import src.auth.users as users_mod

    old_store, real_cls = users_mod._store, users_mod.UserStore
    made: list[int] = []

    class _Counting(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            made.append(1)
            super().__init__(*a, **kw)

    users_mod._store = None
    users_mod.UserStore = _Counting
    try:
        got: list = []
        lock = threading.Lock()

        def _grab():
            store = users_mod.get_user_store()
            with lock:
                got.append(store)

        threads = [threading.Thread(target=_grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(made) == 1, f"应只构造一次，实际 {len(made)} 次"
        assert len({id(s) for s in got}) == 1
    finally:
        users_mod.UserStore = real_cls
        users_mod._store = old_store


def test_container_get_or_create_is_concurrency_safe():
    """并发首访同一 key 只调一次工厂（原实现会重复构造模型/客户端）。"""
    import threading
    import time

    from config.settings import get_settings
    from src.core.container import Container

    c = Container(get_settings())
    calls: list[int] = []
    guard = threading.Lock()

    def factory():
        with guard:
            calls.append(1)
        time.sleep(0.05)  # 放大竞态窗口
        return object()

    out: list = []
    out_lock = threading.Lock()

    def _grab():
        val = c._get_or_create("k", factory)
        with out_lock:
            out.append(val)

    threads = [threading.Thread(target=_grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, f"工厂应只被调用一次，实际 {len(calls)} 次"
    assert len({id(v) for v in out}) == 1


def test_container_nested_factory_does_not_deadlock():
    """工厂内部嵌套取单例（get_retriever→get_reranker）不得自锁死。

    回归背景：加锁后若用普通 Lock，嵌套调用会死锁；必须 RLock。
    """
    import threading

    from config.settings import get_settings
    from src.core.container import Container

    c = Container(get_settings())
    done: list = []

    def work():
        # 模拟 get_retriever 的工厂内部继续调 _get_or_create
        outer = c._get_or_create(
            "outer", lambda: (c._get_or_create("inner", lambda: "I"), "O")
        )
        done.append(outer)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "嵌套单例构造发生死锁（应使用 RLock）"
    assert done == [("I", "O")]
