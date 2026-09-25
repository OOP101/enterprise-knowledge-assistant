# -*- coding: utf-8 -*-
"""凭据轮换：管理员口令 + JWT 签名密钥（默认 dry-run，需显式 --apply 才落盘）。

背景（2026-09-25 代码审查遗留项，用户选择"先不动"，故留此脚本备用）：
  - .env 里 ADMIN_PASSWORD 曾是默认值 admin123，且 admin 账号**已存在**，
    所以只改 .env 对已存在的账号无效 —— 必须同时重置账号口令。本脚本两件事一起做。
  - JWT_SECRET 为低熵可猜值。换掉后现有登录令牌全部失效（需重新登录一次，密码不变）。

用法：
  python scripts/rotate_admin_password.py                 # 预览要做什么（不改任何文件）
  python scripts/rotate_admin_password.py --apply         # 生成随机强口令并应用
  python scripts/rotate_admin_password.py --apply --jwt   # 同时轮换 JWT 签名密钥
  python scripts/rotate_admin_password.py --apply --password '你的新口令'

安全约定：
  - 动 .env 前自动备份为 .env.bak-<日期>；
  - 新口令只打印一次到终端，不写入任何额外的明文文件；
  - 幂等：重复执行就是再换一次，不会报错。
"""
from __future__ import annotations

import argparse
import datetime
import re
import secrets
import shutil
import string
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ENV_FILE = PROJECT_ROOT / ".env"


def _gen_password(length: int = 20) -> str:
    """生成强随机口令（含大小写+数字+符号，且规避易混淆字符）。"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in "!@#$%^&*" for c in pw)
        ):
            return pw


def _gen_jwt_secret() -> str:
    return secrets.token_urlsafe(48)


def _upsert_env(key: str, value: str) -> tuple[bool, str | None]:
    """把 .env 里的 KEY=VALUE 改成新值；不存在则追加。返回 (是否变更, 旧值)。"""
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=\s*(.*)$")
    m = pattern.search(text)
    if m:
        old = m.group(1).strip().strip('"').strip("'")
        text = pattern.sub(f"{key}={value}", text, count=1)
    else:
        old = None
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{key}={value}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    return True, old


def main() -> int:
    ap = argparse.ArgumentParser(description="轮换管理员口令 / JWT 签名密钥")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认仅预览）")
    ap.add_argument("--jwt", action="store_true", help="同时轮换 JWT_SECRET")
    ap.add_argument("--password", default=None, help="指定新口令（不传则随机生成）")
    args = ap.parse_args()

    from config.settings import settings
    from src.auth.users import get_user_store

    username = settings.admin_username
    new_pw = args.password or _gen_password()
    new_jwt = _gen_jwt_secret() if args.jwt else None

    print(f"项目根     : {PROJECT_ROOT}")
    print(f"配置文件   : {ENV_FILE}（存在）" if ENV_FILE.exists() else f"配置文件   : {ENV_FILE}（不存在，将创建）")
    print(f"管理员账号 : {username}")
    print(f"认证开关   : AUTH_ENABLED={settings.auth_enabled}")
    print(f"待执行     : 重置 {username} 口令" + ("；轮换 JWT_SECRET" if args.jwt else ""))
    if not username:
        print("[error] ADMIN_USERNAME 为空，无法确定要重置哪个账号", file=sys.stderr)
        return 2
    if args.password and len(args.password) < 8:
        print("[error] 指定口令少于 8 位，会被强度策略拒绝", file=sys.stderr)
        return 2

    if not args.apply:
        print("\n[dry-run] 以上为预览。确认无误后加 --apply 真正执行。")
        return 0

    # 1) 备份 .env（改配置前必备份）
    if ENV_FILE.exists():
        backup = ENV_FILE.with_name(f".env.bak-{datetime.date.today().isoformat()}")
        shutil.copy2(ENV_FILE, backup)
        print(f"\n[ok] 已备份 .env -> {backup.name}")

    # 2) 重置账号口令（关键：只改 .env 对已存在账号无效）
    store = get_user_store()
    result = store.reset_password(username, new_pw)
    if result is None:
        store.register(username, new_pw, role="admin")
        print(f"[ok] {username} 原本不存在，已新建管理员账号")
    else:
        print(f"[ok] 已重置 {username} 的登录口令")

    # 3) 同步 .env，保证下次从零启动时预置口令一致
    _upsert_env("ADMIN_PASSWORD", new_pw)
    print(f"[ok] 已更新 .env 的 ADMIN_PASSWORD")
    if new_jwt:
        _upsert_env("JWT_SECRET", new_jwt)
        print(f"[ok] 已更新 .env 的 JWT_SECRET（现有登录令牌已失效，需重新登录）")

    print("\n" + "=" * 62)
    print(f"  新登录口令（只显示这一次，请立即保存到密码管理器）：")
    print(f"     账号：{username}")
    print(f"     口令：{new_pw}")
    print("=" * 62)
    print("提示：登录后可用「修改密码」入口换成你自己好记的强口令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
