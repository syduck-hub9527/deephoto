"""供本地前端使用的状态摘要；只投影公开配置，不序列化配置对象。"""

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status")
def system_status(request: Request):
    settings = request.app.state.settings
    index = request.app.state.index_service
    hybrid = index.embeddings is not None
    return {
        "chat_model": settings.chat_model,
        "retrieval": {
            "mode": "hybrid" if hybrid else "keyword",
            "embedding_model": settings.embedding_model if hybrid else None,
            "embedding_version": index.embedding_version if hybrid else None,
        },
        "description_enabled": settings.description_enabled,
        "hitl": {"enabled": settings.qa_hitl_enabled,
                 "auto_approve_images": settings.qa_hitl_auto_approve_images},
        "features": {"subagents": settings.qa_subagents_enabled,
                     "kb_vfs": settings.qa_kb_vfs_enabled,
                     "skills": settings.qa_skills_enabled},
    }
