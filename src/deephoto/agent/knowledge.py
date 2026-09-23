"""知识库服务:Deep Agents 工具背后的业务服务(开发文档§5.1)。

职责:
- 按请求上下文确定可检索文档(权限过滤,模型无权指定任意租户);
- 融合检索 + 沿 ChunkImageLink 补全显式关联图文;
- 为 inspect_image 提供多模态内容块(真实原图,非 URL)。
"""

from __future__ import annotations

import base64
import logging
from sqlite3 import Connection

from .. import repo
from ..config import Settings
from ..parsing.captions import find_referenced_figures
from ..pipeline.linking import CONF_DISPLAY_THRESHOLD
from ..security import AuthContext, sign_resource
from ..storage import ObjectStore

logger = logging.getLogger(__name__)

_SNIPPET_CHARS = 320


class KnowledgeService:
    def __init__(self, settings: Settings, store: ObjectStore, index_service):
        self.settings = settings
        self.store = store
        self.index_service = index_service

    # ---- 权限 ----

    def allowed_document_ids(self, conn: Connection, ctx: AuthContext,
                             document_id: str | None = None) -> list[str]:
        """当前用户可检索的 ready 文档;指定 document_id 时校验归属。"""
        if document_id:
            doc = repo.get_owned_document(conn, document_id, ctx.tenant_id)
            return [document_id] if doc and doc["status"] == "ready" else []
        rows = conn.execute(
            "SELECT id FROM documents WHERE tenant_id = ? AND status = 'ready'", (ctx.tenant_id,),
        ).fetchall()
        return [r["id"] for r in rows]

    # ---- 检索工具(search_knowledge 的实现)----

    def search(self, conn: Connection, ctx: AuthContext, query: str,
               document_id: str | None = None) -> dict:
        doc_ids = self.allowed_document_ids(conn, ctx, document_id)
        if not doc_ids:
            return {"error": "没有可检索的文档(不存在、无权限或尚未处理完成)", "chunks": [], "images": []}
        if document_id and document_id not in doc_ids:
            return {"error": f"文档 {document_id} 不可访问", "chunks": [], "images": []}

        ranked = self.index_service.search(conn, document_ids=doc_ids, query=query)
        chunk_hits = {c["source_id"]: c for c in ranked["chunks"]}
        image_hits = {i["source_id"]: i for i in ranked["images"]}

        chunks = {c["id"]: c for c in repo.get_chunks(conn, list(chunk_hits))}
        occs: dict[str, dict] = {}

        # 命中正文 -> 补全显式关联图片;邻近关系仅作候选
        chunk_image_rel: dict[str, tuple[str, float]] = {}
        for link in repo.links_for_chunks(conn, list(chunks)):
            if link["confidence"] >= CONF_DISPLAY_THRESHOLD:
                if link["image_occurrence_id"] not in chunk_image_rel:
                    chunk_image_rel[link["image_occurrence_id"]] = (link["relation"], link["confidence"])
        # 命中图片 -> 补全图注与解释它的正文
        for link in repo.links_for_images(conn, list(image_hits)):
            if link["relation"] in ("caption_of", "references") and link["chunk_id"] not in chunks:
                extra = repo.get_chunks(conn, [link["chunk_id"]])
                for c in extra:
                    chunks[c["id"]] = c

        wanted_occ_ids = set(image_hits) | set(chunk_image_rel)
        if wanted_occ_ids:
            for occ_id in wanted_occ_ids:
                occ = repo.get_occurrence(conn, occ_id)
                if occ and occ["tenant_id"] == ctx.tenant_id:
                    occs[occ_id] = occ

        # 显式图号兜底:“图 3”类问题直接锚定
        for number in find_referenced_figures(query):
            for doc_id in doc_ids:
                for occ in repo.occurrences_for_document(conn, doc_id):
                    if occ["figure_number"] == number and occ["id"] not in occs:
                        occs[occ["id"]] = occ
                        chunk_image_rel[occ["id"]] = ("explicit_figure", 1.0)

        return {
            "chunks": [
                {
                    "chunk_id": c["id"], "document_id": c["document_id"],
                    "section": c["section"], "page_start": c["page_start"], "page_end": c["page_end"],
                    "score": chunk_hits.get(c["id"], {}).get("score"),
                    "text": c["text"][:_SNIPPET_CHARS] + ("…" if len(c["text"]) > _SNIPPET_CHARS else ""),
                }
                for c in chunks.values()
            ],
            "images": [
                {
                    "image_occurrence_id": o["id"], "document_id": o["document_id"],
                    "figure_number": o["figure_number"], "page": o["page_number"],
                    "caption": o["caption"], "description": o["description"],
                    "relation": chunk_image_rel.get(o["id"], ("matched_image", None))[0],
                    "needs_review": o["needs_review"],
                }
                for o in occs.values()
            ],
        }

    # ---- 看图工具(inspect_image 的实现)----

    def image_content_blocks(self, conn: Connection, ctx: AuthContext, occ_id: str) -> list[dict]:
        occ = repo.get_occurrence(conn, occ_id)
        if occ is None or occ["tenant_id"] != ctx.tenant_id:
            return [{"type": "text", "text": f"图片 {occ_id} 不存在或无权限访问"}]
        asset = repo.get_asset(conn, occ["image_asset_id"])
        image_bytes = self.store.get(asset["original_object_key"])
        header = (
            f"图片 {occ_id}(图号:{occ['figure_number'] or '无'},"
            f"第 {occ['page_number']} 页)\n图注:{occ['caption'] or '无'}"
        )
        return [
            {"type": "text", "text": header},
            {"type": "image", "base64": base64.b64encode(image_bytes).decode(),
             "mime_type": asset["mime_type"]},
        ]

    # ---- 回答后端的图片组装(带短时签名 URL)----

    def build_image_entries(self, conn: Connection, ctx: AuthContext, occ_ids: list[str]) -> list[dict]:
        entries: list[dict] = []
        seen_assets: set[str] = set()
        for occ_id in occ_ids:
            occ = repo.get_occurrence(conn, occ_id)
            if occ is None or occ["tenant_id"] != ctx.tenant_id:
                continue    # 丢弃无效/越权引用(§6)
            if occ["image_asset_id"] in seen_assets:
                continue    # 同图多处出现时按命中上下文取第一个正确出处
            seen_assets.add(occ["image_asset_id"])
            doc_id = occ["document_id"]
            exp1, sig1 = sign_resource(self.settings.secret_key, doc_id, f"images/{occ_id}")
            exp2, sig2 = sign_resource(self.settings.secret_key, doc_id, f"pages/{occ['page_number']}")
            entries.append({
                "image_occurrence_id": occ_id,
                "document_id": doc_id,
                "figure_number": occ["figure_number"],
                "caption": occ["caption"],
                "page": occ["page_number"],
                "image_url": f"/api/documents/{doc_id}/images/{occ_id}?expires={exp1}&sig={sig1}",
                "source_page_url": f"/api/documents/{doc_id}/pages/{occ['page_number']}?expires={exp2}&sig={sig2}",
            })
        return entries
