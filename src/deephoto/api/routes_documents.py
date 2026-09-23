"""文档路由:上传、状态、图片与页面资源(带鉴权/签名)。

对应开发文档§3.1、§5.3:图片接口再次检查权限,
不允许靠猜测 image_occurrence_id 获取其他文档图片。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from .. import repo
from ..security import AuthContext, verify_resource_signature
from .deps import CtxDep, conn_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.post("")
async def upload_document(request: Request, file: UploadFile, ctx: AuthContext = CtxDep):
    settings = request.app.state.settings
    conn = conn_for(request)
    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.max_upload_mb}MB 限制")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="文件内容不是有效 PDF")

    object_key, digest = request.app.state.store.put(data, "pdfs", "application/pdf")
    doc_id = repo.insert_document(
        conn, tenant_id=ctx.tenant_id, owner_id=ctx.user_id, filename=filename,
        pdf_object_key=object_key, sha256=digest,
        ingestion_version=settings.ingestion_version,
    )
    return {"document_id": doc_id, "status": "queued"}


@router.get("")
def list_documents(request: Request, ctx: AuthContext = CtxDep):
    return {"documents": repo.list_documents(conn_for(request), ctx.tenant_id)}


@router.get("/{document_id}")
def document_detail(request: Request, document_id: str, ctx: AuthContext = CtxDep):
    doc = repo.get_owned_document(conn_for(request), document_id, ctx.tenant_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    doc.pop("pdf_object_key", None)
    return doc


@router.delete("/{document_id}")
def delete_document(request: Request, document_id: str, ctx: AuthContext = CtxDep):
    result = repo.delete_document(conn_for(request), document_id, ctx.tenant_id)
    if result is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    store = request.app.state.store
    for key in result["object_keys"]:
        try:
            store.delete_if_unreferenced(key)
        except OSError:
            logger.warning("failed to remove object %s", key)
    return {"deleted": document_id}


# ---- 图片与页面资源:Bearer 鉴权或短时签名 URL 二选一 ----

def _authorize_resource(request: Request, conn, document_id: str, resource: str,
                        expires: int | None, sig: str | None) -> None:
    settings = request.app.state.settings
    if expires and sig:
        if verify_resource_signature(settings.secret_key, document_id, resource, expires, sig):
            return
        raise HTTPException(status_code=403, detail="签名无效或已过期")
    # 无签名则要求 Bearer 鉴权 + 租户归属
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        from ..security import resolve_token
        ctx = resolve_token(conn, authorization.split(None, 1)[1].strip())
        if ctx and repo.get_owned_document(conn, document_id, ctx.tenant_id):
            return
    raise HTTPException(status_code=401, detail="需要 Bearer 令牌或有效签名")


@router.get("/{document_id}/images/{occurrence_id}")
def get_image(request: Request, document_id: str, occurrence_id: str,
              expires: int | None = Query(default=None), sig: str | None = Query(default=None)):
    conn = conn_for(request)
    _authorize_resource(request, conn, document_id, f"images/{occurrence_id}", expires, sig)
    occ = repo.get_occurrence(conn, occurrence_id)
    if occ is None or occ["document_id"] != document_id:
        raise HTTPException(status_code=404, detail="图片不存在")
    asset = repo.get_asset(conn, occ["image_asset_id"])
    data = request.app.state.store.get(asset["original_object_key"])
    return Response(content=data, media_type=asset["mime_type"],
                    headers={"Cache-Control": "private, max-age=600"})


@router.get("/{document_id}/pages/{page_number}")
def get_page_preview(request: Request, document_id: str, page_number: int,
                     expires: int | None = Query(default=None), sig: str | None = Query(default=None)):
    conn = conn_for(request)
    _authorize_resource(request, conn, document_id, f"pages/{page_number}", expires, sig)
    doc = repo.get_document(conn, document_id)
    if doc is None or page_number < 1 or (doc["page_count"] and page_number > doc["page_count"]):
        raise HTTPException(status_code=404, detail="页面不存在")
    store = request.app.state.store
    pdf_bytes = store.get(doc["pdf_object_key"])
    try:
        png = request.app.state.parser.render_page(pdf_bytes, page_number)
    except Exception:
        raise HTTPException(status_code=404, detail="页面渲染失败")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=600"})
