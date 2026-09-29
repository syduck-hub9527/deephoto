"""文档路由:上传、状态、图片与页面资源。

本地单机部署,无鉴权;租户隔离结构保留(固定租户,见 security.LOCAL_CTX)。
列表/详情附带入库观测进度(独立观测库,见 progress_store.py)。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response

from .. import repo
from ..parsing.formats import UnsupportedFormat, accept_extensions, detect_format
from ..parsing.registry import EngineUnavailable, engine_for
from ..security import AuthContext, new_id
from .deps import CtxDep, conn_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _sanitize_filename(raw: str | None) -> str:
    """只取 basename,去控制字符,限长 200;原样入库展示(前端已 esc)。"""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:200] or "document"


# 内容-level 拒绝(扩展名与内容不符/加密/损坏/空/二进制/不可解码)-> 400;
# 其余(格式本身不支持:未知扩展名、非 OOXML 的 zip 等)-> 415 Unsupported Media Type
_FORMAT_400_CODES = {"empty", "mismatch", "encrypted", "corrupt", "binary", "undecodable"}


@router.post("")
async def upload_document(request: Request, file: UploadFile, ctx: AuthContext = CtxDep):
    settings = request.app.state.settings
    conn = conn_for(request)
    filename = _sanitize_filename(file.filename)
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.max_upload_mb}MB 限制")
    try:
        fmt = detect_format(data, filename)
    except UnsupportedFormat as exc:
        raise HTTPException(status_code=400 if exc.code in _FORMAT_400_CODES else 415,
                            detail=str(exc))
    if fmt.key not in settings.allowed_formats:
        raise HTTPException(status_code=415, detail=f"该格式({fmt.key})不在允许上传的白名单内")
    try:
        engine = engine_for(fmt, settings)
    except EngineUnavailable as exc:
        raise HTTPException(status_code=415, detail=str(exc))

    object_key, digest = request.app.state.store.put(data, "sources", fmt.mime, ext=fmt.ext)
    doc_id = repo.insert_document(
        conn, tenant_id=ctx.tenant_id, owner_id=ctx.user_id, filename=filename,
        source_object_key=object_key, sha256=digest,
        ingestion_version=settings.ingestion_version,
        source_format=fmt.key, locator_kind=fmt.locator_kind, parse_engine=engine,
    )
    progress = getattr(request.app.state, "progress_store", None)
    if progress is not None:
        # 每次真正的新入库尝试登记独立 run;排队耗时从此刻起算
        progress.register_run(new_id("run"), doc_id, ctx.tenant_id)
    return {"document_id": doc_id, "status": "queued", "source_format": fmt.key}


@router.get("")
def list_documents(request: Request, ctx: AuthContext = CtxDep):
    docs = repo.list_documents(conn_for(request), ctx.tenant_id)
    progress = getattr(request.app.state, "progress_store", None)
    if progress is not None:
        # 批量合并轻量摘要;无观测记录的旧文档 progress 为 None,不填造零耗时
        summaries = progress.summaries(ctx.tenant_id, docs)
        for doc in docs:
            doc["progress"] = summaries.get(doc["id"])
    else:
        for doc in docs:
            doc["progress"] = None
    return {"documents": docs}


@router.get("/upload-config")
def upload_config(request: Request):
    """上传白名单(accept 扩展名列表):前端启动时读取,与服务端 ALLOWED_FORMATS 一致。
    注册在 /{document_id} 之前,避免被文档详情路由捕获。"""
    settings = request.app.state.settings
    return {"accept": accept_extensions(settings.allowed_formats),
            "formats": sorted(settings.allowed_formats)}


@router.get("/{document_id}")
def document_detail(request: Request, document_id: str, ctx: AuthContext = CtxDep):
    doc = repo.get_owned_document(conn_for(request), document_id, ctx.tenant_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    doc.pop("source_object_key", None)
    progress = getattr(request.app.state, "progress_store", None)
    if progress is not None:
        # 归属校验(上方 get_owned_document)之后再读同 tenant 的观测数据;
        # 传入真实业务状态,与列表接口共用同一套终态判断
        doc["progress"] = progress.detail(ctx.tenant_id, document_id, doc["status"])
    else:
        doc["progress"] = None
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
    progress = getattr(request.app.state, "progress_store", None)
    if progress is not None:
        progress.cleanup_documents([document_id])   # 在途回调发现 run 不存在时不会重建
    return {"deleted": document_id}


# ---- 图片与页面资源 ----

@router.get("/{document_id}/images/{occurrence_id}")
def get_image(request: Request, document_id: str, occurrence_id: str):
    conn = conn_for(request)
    occ = repo.get_occurrence(conn, occurrence_id)
    if occ is None or occ["document_id"] != document_id:
        raise HTTPException(status_code=404, detail="图片不存在")
    asset = repo.get_asset(conn, occ["image_asset_id"])
    data = request.app.state.store.get(asset["original_object_key"])
    return Response(content=data, media_type=asset["mime_type"],
                    headers={"Cache-Control": "private, max-age=600"})


@router.get("/{document_id}/pages/{page_number}")
def get_page_preview(request: Request, document_id: str, page_number: int):
    conn = conn_for(request)
    doc = repo.get_document(conn, document_id)
    if doc is None or page_number < 1 or (doc["page_count"] and page_number > doc["page_count"]):
        raise HTTPException(status_code=404, detail="页面不存在")
    if doc.get("source_format") != "pdf":
        # 非 PDF 没有可渲染的页(虚拟分段/幻灯片等);图片格式走 P3 再说
        raise HTTPException(status_code=404, detail="该格式不提供页预览")
    store = request.app.state.store
    pdf_bytes = store.get(doc["source_object_key"])
    try:
        png = request.app.state.parser.render_page(pdf_bytes, page_number)
    except Exception:
        raise HTTPException(status_code=404, detail="页面渲染失败")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=600"})
