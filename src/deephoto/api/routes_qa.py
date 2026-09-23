"""问答路由(开发文档§5.3)。

/api/qa 为一次性响应;/api/qa/stream 为 SSE 流式(token 实时推送,
结束时 done 事件携带后端校验后的 citations/images)。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..security import AuthContext
from .deps import CtxDep, conn_for

router = APIRouter(prefix="/api", tags=["qa"])


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    document_id: str | None = None
    # 多轮对话历史:[{"role": "user"|"assistant", "content": str}],服务端只取最近 20 条
    history: list[dict] = Field(default_factory=list, max_length=50)


@router.post("/qa")
def ask(request: Request, body: Question, ctx: AuthContext = CtxDep):
    qa_service = request.app.state.qa_service
    try:
        result = qa_service.answer(conn_for(request), ctx, body.question, body.document_id,
                                   history=body.history)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if result.get("answer") is None and result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/qa/stream")
def ask_stream(request: Request, body: Question, ctx: AuthContext = CtxDep):
    qa_service = request.app.state.qa_service
    events = qa_service.answer_stream(ctx, body.question, body.document_id, history=body.history)

    def sse():
        for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
