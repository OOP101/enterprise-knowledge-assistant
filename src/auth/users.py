"""用户存储与密码管理（JSON 文件 + 加盐慢哈希）。"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

from config.settings import settings

DEFAULT_USERS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "users.json"

_ROLE_DEFAULT = "user"
_ROLE_ADMIN = "admin"

_roles = {"user": _ROLE_DEFAULT, "admin": _ROLE_ADMIN}

# 密码安全策略
MIN_PASSWORD_LEN = 8
# scrypt 参数：N=2^14 约 50ms/次，仅登录/注册时调用，可接受
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_PREFIX = "scrypt$"


def _hash_password(password: str, salt: str) -> str:
    """scrypt 慢哈希（自适应成本，抗 GPU 暴力破解）。

    返回格式：`scrypt$<hash_hex>`；盐独立存于用户记录的 salt 字段。
    """
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return f"{_SCRYPT_PREFIX}{dk.hex()}"


def _legacy_hash(password: str, salt: str) -> str:
    """v2.3.1 之前的旧格式：单次加盐 SHA-256（64 位 hex）。仅为兼容存量用户保留。"""
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def _verify_password(password: str, salt: str, stored: str) -> bool:
    """按存储格式分流校验：scrypt$ 前缀走慢哈希，64 位 hex 走旧 SHA-256。"""
    if stored.startswith(_SCRYPT_PREFIX):
        expected = stored[len(_SCRYPT_PREFIX):]
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
        )
        return hmac.compare_digest(dk.hex(), expected)
    return hmac.compare_digest(_legacy_hash(password, salt), stored)


def _validate_password(password: str) -> None:
    if not password:
        raise ValueError("密码不能为空")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码长度至少 {MIN_PASSWORD_LEN} 位")


class UserStore:
    """基于 JSON 文件的用户存储（线程安全）。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_USERS_FILE
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                self._users = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self._users = {}

    def _save(self) -> None:
        # 原子写：先写临时文件再 os.replace，避免进程中断产生半截 JSON
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._users, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    def register(
        self,
        username: str,
        password: str,
        role: str = _ROLE_DEFAULT,
        department: str = "",
        extra_kbs: list[str] | None = None,
    ) -> dict:
        """注册用户，返回用户信息（不含密码哈希）。

        - department: 用户所属部门（决定默认可访问的知识库）
        - extra_kbs: 额外单独授权的知识库（个性化覆盖，方便调部门/临时授权）
        """
        username = username.strip()
        _validate_password(password)
        if not username:
            raise ValueError("用户名不能为空")
        with self._lock:
            if username in self._users:
                raise ValueError("用户名已存在")
            salt = secrets.token_hex(16)
            self._users[username] = {
                "username": username,
                "role": role if role in _roles else _ROLE_DEFAULT,
                "department": department.strip(),
                "extra_kbs": extra_kbs or [],
                "salt": salt,
                "password_hash": _hash_password(password, salt),
                "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            self._save()
            return self._public(username)

    def update(self, username: str, *, department: str | None = None,
               role: str | None = None, extra_kbs: list[str] | None = None) -> dict | None:
        """更新用户信息（主要用于调部门 / 调整知识库授权）。

        返回更新后的用户信息；用户不存在返回 None。
        """
        username = username.strip()
        with self._lock:
            if username not in self._users:
                return None
            u = self._users[username]
            if department is not None:
                u["department"] = department.strip()
            if role is not None and role in _roles:
                u["role"] = role
            if extra_kbs is not None:
                u["extra_kbs"] = list(extra_kbs)
            self._save()
            return self._public(username)

    def reset_password(self, username: str, new_password: str) -> dict | None:
        """重置用户密码（重新生成盐 + 哈希）。

        返回更新后的用户信息；用户不存在返回 None。
        """
        username = username.strip()
        _validate_password(new_password)
        with self._lock:
            if username not in self._users:
                return None
            u = self._users[username]
            salt = secrets.token_hex(16)
            u["salt"] = salt
            u["password_hash"] = _hash_password(new_password, salt)
            self._save()
            return self._public(username)

    def verify(self, username: str, password: str) -> dict | None:
        """校验用户名密码，成功返回用户信息。

        旧 SHA-256 哈希校验通过后惰性升级为 scrypt（下次登录完成迁移）。
        """
        with self._lock:
            user = self._users.get(username.strip())
            if not user:
                return None
            if not _verify_password(password, user["salt"], user["password_hash"]):
                return None
            if not user["password_hash"].startswith(_SCRYPT_PREFIX):
                user["salt"] = secrets.token_hex(16)
                user["password_hash"] = _hash_password(password, user["salt"])
                self._save()
            return self._public(username)

    def get(self, username: str) -> dict | None:
        with self._lock:
            return self._public(username) if username in self._users else None

    def list(self) -> list[dict]:
        with self._lock:
            return [self._public(u) for u in self._users]

    def _public(self, username: str) -> dict:
        u = self._users[username]
        return {
            "username": u["username"],
            "role": u["role"],
            "department": u.get("department", ""),
            "extra_kbs": u.get("extra_kbs", []),
            "created_at": u.get("created_at", ""),
        }


_store: UserStore | None = None


def get_user_store() -> UserStore:
    global _store
    if _store is None:
        _store = UserStore()
    return _store
