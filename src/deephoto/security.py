"""本地单机部署的身份上下文与 ID 生成。

项目仅本人在本地访问,无鉴权:所有请求归属同一个固定租户。
tenant_id 沿用历史 bootstrap 默认值 "default",已有数据库中的文档保持可见。
历史库中残留的 users 表与令牌字段不再使用。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    user_id: str


# 固定租户上下文:所有请求共享,不再校验任何令牌。
LOCAL_CTX = AuthContext(tenant_id="default", user_id="admin")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"
