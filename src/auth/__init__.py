"""认证与权限（2.0）。

- JWT 令牌签发/校验（pyjwt 可用则用之，否则回退标准库 HMAC 签名）
- 用户存储（JSON 文件，密码 bcrypt/hashlib 加盐）
- RBAC：admin / user 角色
- FastAPI 依赖注入：get_current_user / require_role
"""
