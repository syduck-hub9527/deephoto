"""FastAPI 依赖:鉴权上下文与当前线程连接获取。

注意:sqlite 连接有线程亲和性(只能在创建它的线程内使用)。
FastAPI 同步依赖在线程池执行,而 async 端点体在事件循环线程执行,
因此连接不得作为依赖注入传递。端点函数体内用 conn_for(request)
获取当前线程自己的连接(thread-local 缓存,见 db.py)。
"""

from __future__ import annotations

from sqlite3 import Connection

from fastapi import Depends, Header, HTTPException, Request

from ..db import connect
from ..security import AuthContext, resolve_token


def conn_for(request: Request) -> Connection:
    """当前请求处理线程自己的 sqlite 连接。只能在端点/依赖函数体内调用。"""
    return connect(request.app.state.settings.db_path)


def get_ctx(request: Request, authorization: str | None = Header(default=None)) -> AuthContext:
    """Bearer 令牌 -> 请求上下文。无效令牌一律 401。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <token>")
    token = authorization.split(None, 1)[1].strip()
    ctx = resolve_token(conn_for(request), token)
    if ctx is None:
        raise HTTPException(status_code=401, detail="无效令牌")
    return ctx


CtxDep = Depends(get_ctx)
