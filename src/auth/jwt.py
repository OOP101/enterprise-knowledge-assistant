"""JWT 签发与校验。

优先使用 pyjwt；未安装时回退标准库 HMAC-SHA256 签名（结构兼容 JWT）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from config.settings import settings


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _signature(header_b64: str, payload_b64: str, secret: str) -> str:
    msg = f"{header_b64}.{payload_b64}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return _b64url(sig)


def create_token(payload: dict[str, Any]) -> str:
    """签发 JWT。"""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    full_payload = {
        **payload,
        "iat": now,
        "exp": now + settings.jwt_expire_minutes * 60,
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url(
        json.dumps(full_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    sig = _signature(header_b64, payload_b64, settings.jwt_secret)
    return f"{header_b64}.{payload_b64}.{sig}"


def decode_token(token: str) -> dict[str, Any] | None:
    """校验 JWT 并返回 payload；非法/过期返回 None。"""
    try:
        header_b64, payload_b64, sig = token.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(
        _signature(header_b64, payload_b64, settings.jwt_secret), sig
    ):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload
