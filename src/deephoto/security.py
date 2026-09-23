"""开发级鉴权:Bearer 令牌 -> 请求上下文(tenant_id / user_id)。

生产环境应替换为真实身份系统;但权限过滤边界(tenant 隔离、
文档归属校验)在本模块之后的所有层都已按多租户实现。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from sqlite3 import Connection


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    user_id: str


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def ensure_bootstrap_user(conn: Connection, tenant_id: str, user_name: str, token: str) -> None:
    """启动时种入开发用户(幂等)。"""
    row = conn.execute("SELECT id FROM users WHERE token = ?", (token,)).fetchone()
    if row:
        return
    conn.execute(
        "INSERT INTO users (id, tenant_id, name, token, created_at) VALUES (?,?,?,?,?)",
        (new_id("usr"), tenant_id, user_name, token, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
    )
    conn.commit()


def resolve_token(conn: Connection, token: str) -> AuthContext | None:
    row = conn.execute("SELECT id, tenant_id FROM users WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None
    return AuthContext(tenant_id=row["tenant_id"], user_id=row["id"])


# ---- 短时签名图片 URL(能力凭证,供前端 <img> 直接使用)----

def sign_resource(secret_key: str, document_id: str, resource: str, ttl_seconds: int = 600) -> tuple[int, str]:
    """返回 (过期时间戳, 签名)。resource 形如 'images/occ_xxx' 或 'pages/4'。"""
    expires = int(time.time()) + ttl_seconds
    payload = f"{document_id}:{resource}:{expires}"
    sig = hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return expires, sig


def verify_resource_signature(
    secret_key: str, document_id: str, resource: str, expires: int, sig: str
) -> bool:
    if int(time.time()) > expires:
        return False
    payload = f"{document_id}:{resource}:{expires}"
    expected = hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)
