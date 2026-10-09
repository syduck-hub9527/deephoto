"""问答路由(开发文档§5.3)。

/api/qa 为一次性响应;/api/qa/stream 为 SSE 流式(token 实时推送,
结束时 done 事件携带后端校验后的 citations/images)。
"""

from __future__ import annotations

import json

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..security import AuthContext
from ..agent.persistence import SessionError
from ..agent.context import Preferences
from .deps import CtxDep, conn_for

router = APIRouter(prefix="/api", tags=["qa"])


class _QAStreamingResponse(StreamingResponse):
    """断连/发送失败后主动关闭问答生成器,不依赖 GC 释放会话租约。"""

    def __init__(self, content, *, qa_events, **kwargs):
        super().__init__(content, **kwargs)
        self._qa_events = qa_events

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self._qa_events, "close", None)
            if close is not None:
                with CancelScope(shield=True):
                    await run_in_threadpool(close)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    document_id: str | None = None
    # 多轮对话历史:[{"role": "user"|"assistant", "content": str}],服务端只取最近 20 条
    history: list[dict] = Field(default_factory=list, max_length=50)
    session_id: str | None = Field(default=None, pattern=r"^sess_[0-9a-f]{32}$")


@router.post("/qa")
def ask(request: Request, body: Question, ctx: AuthContext = CtxDep):
    qa_service = request.app.state.qa_service
    try:
        result = qa_service.answer(conn_for(request), ctx, body.question, body.document_id,
                                   history=body.history, session_id=body.session_id)
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if result.get("answer") is None and result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/qa/stream")
def ask_stream(request: Request, body: Question, ctx: AuthContext = CtxDep):
    qa_service = request.app.state.qa_service
    events = qa_service.answer_stream(ctx, body.question, body.document_id,
                                     history=body.history, session_id=body.session_id)

    def sse():
        for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return _QAStreamingResponse(
        sse(),
        qa_events=events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/qa/sessions/{session_id}")
def delete_session(request: Request, session_id: str, ctx: AuthContext = CtxDep):
    try:
        request.app.state.qa_service.delete_session(ctx, session_id)
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"deleted": True}


# 05:用户显式管理表达偏好。不是模型工具,不接受自由文本指令。
def _memory(request: Request):
    try:
        return request.app.state.qa_service.preference_memory()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/qa/memory")
def get_memory(request: Request, ctx: AuthContext = CtxDep):
    return _memory(request).get(ctx)


@router.put("/qa/memory")
def put_memory(request: Request, body: Preferences, ctx: AuthContext = CtxDep):
    return _memory(request).put(ctx, body)


@router.delete("/qa/memory")
def delete_memory(request: Request, ctx: AuthContext = CtxDep):
    _memory(request).delete(ctx)
    return {"deleted": True}
