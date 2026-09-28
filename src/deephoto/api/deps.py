"""FastAPI 依赖:固定租户上下文与当前线程连接获取。

本地单机部署,无鉴权:get_ctx 直接返回 LOCAL_CTX。
注意:sqlite 连接有线程亲和性(只能在创建它的线程内使用)。
FastAPI 同步依赖在线程池执行,而 async 端点体在事件循环线程执行,
因此连接不得作为依赖注入传递。端点函数体内用 conn_for(request)
获取当前线程自己的连接(thread-local 缓存,见 db.py)。
"""

from __future__ import annotations

from sqlite3 import Connection

from fastapi import Depends, Request

from ..db import connect
from ..security import LOCAL_CTX, AuthContext


def conn_for(request: Request) -> Connection:
    """当前请求处理线程自己的 sqlite 连接。只能在端点/依赖函数体内调用。"""
    return connect(request.app.state.settings.db_path)


def get_ctx() -> AuthContext:
    """本地单机:所有请求共享固定租户上下文。"""
    return LOCAL_CTX


CtxDep = Depends(get_ctx)
