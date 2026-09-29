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
from ..parsing.captions import find_figure_mentions
from ..pipeline.linking import CONF_DISPLAY_THRESHOLD
from ..security import AuthContext
from ..storage import ObjectStore
from .locator import locator_label

logger = logging.getLogger(__name__)

_SNIPPET_CHARS = 320
# 单次检索返回给模型的正文总字符预算:预算内的命中块给全文,超出的只给开头并标 truncated,
# 模型可再调用 read_chunk 读取全文。块长有硬上限(MAX_CHARS),预算约可容纳 6 个满长块。
_FULL_TEXT_BUDGET = 8000
_MAX_NEIGHBORS = 2


def _chunk_entry(c: dict, docs: dict[str, dict]) -> dict:
    """检索/读块的统一输出:带文件名、格式与位置文案;section 类文档省略页码
    (页码是虚拟分段号,给了模型会说"第 N 页",§3.9-3)。"""
    doc = docs.get(c["document_id"]) or {}
    kind = doc.get("locator_kind", "page")
    entry = {
        "chunk_id": c["id"], "document_id": c["document_id"],
        "document": doc.get("filename"), "source_format": doc.get("source_format"),
        "section": c["section"],
        "locator": locator_label(kind, c["page_start"], c["page_end"], c["section"]),
    }
    if kind != "section":
        entry["page_start"] = c["page_start"]
        entry["page_end"] = c["page_end"]
    return entry


class KnowledgeService:
    def __init__(self, store: ObjectStore, index_service):
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
        for number in find_figure_mentions(query):
            for doc_id in doc_ids:
                for occ in repo.occurrences_for_document(conn, doc_id):
                    if occ["figure_number"] == number and occ["id"] not in occs:
                        occs[occ["id"]] = occ
                        chunk_image_rel[occ["id"]] = ("explicit_figure", 1.0)

        # 按分数从高到低在预算内给全文;补充进来的关联块(无分数)排最后
        ordered = sorted(chunks.values(),
                         key=lambda c: chunk_hits.get(c["id"], {}).get("score") or 0.0, reverse=True)
        docs = repo.documents_brief(conn, [c["document_id"] for c in ordered]
                                    + [o["document_id"] for o in occs.values()])
        budget = _FULL_TEXT_BUDGET
        chunk_entries: list[dict] = []
        for c in ordered:
            text, truncated = c["text"], False
            if len(text) <= budget:
                budget -= len(text)
            else:
                text, truncated = text[:_SNIPPET_CHARS] + "…", True
            entry = _chunk_entry(c, docs)
            entry.update({
                "score": chunk_hits.get(c["id"], {}).get("score"),
                "text": text, "truncated": truncated,
            })
            chunk_entries.append(entry)

        return {
            "chunks": chunk_entries,
            "images": [
                {
                    "image_occurrence_id": o["id"], "document_id": o["document_id"],
                    "figure_number": o["figure_number"], "page": o["page_number"],
                    "locator_label": locator_label(
                        (docs.get(o["document_id"]) or {}).get("locator_kind", "page"),
                        o["page_number"]),
                    "caption": o["caption"], "description": o["description"],
                    "relation": chunk_image_rel.get(o["id"], ("matched_image", None))[0],
                    "needs_review": o["needs_review"],
                }
                for o in occs.values()
            ],
        }

    # ---- 读全文工具(read_chunk 的实现)----

    def read_chunk(self, conn: Connection, ctx: AuthContext, chunk_id: str,
                   neighbors: int = 0) -> dict:
        """读取某个正文块的完整文本;neighbors>0 时一并返回前后相邻块(阅读顺序)。"""
        chunk = next(iter(repo.get_chunks(conn, [chunk_id])), None)
        if chunk is None or chunk["tenant_id"] != ctx.tenant_id \
                or not self.allowed_document_ids(conn, ctx, chunk["document_id"]):
            return {"error": f"正文块 {chunk_id} 不存在或无权限访问", "chunks": []}
        neighbors = max(0, min(int(neighbors or 0), _MAX_NEIGHBORS))
        ordered = repo.chunks_in_order(conn, chunk["document_id"])
        index = next((i for i, c in enumerate(ordered) if c["id"] == chunk_id), 0)
        window = ordered[max(0, index - neighbors): index + neighbors + 1]
        docs = repo.documents_brief(conn, [chunk["document_id"]])
        return {"chunks": [
            dict(_chunk_entry(c, docs), text=c["text"],
                 role="target" if c["id"] == chunk_id else "neighbor")
            for c in window
        ]}

    # ---- 看图工具(inspect_image 的实现)----

    def image_content_blocks(self, conn: Connection, ctx: AuthContext, occ_id: str) -> list[dict]:
        occ = repo.get_occurrence(conn, occ_id)
        if occ is None or occ["tenant_id"] != ctx.tenant_id:
            return [{"type": "text", "text": f"图片 {occ_id} 不存在或无权限访问"}]
        asset = repo.get_asset(conn, occ["image_asset_id"])
        image_bytes = self.store.get(asset["original_object_key"])
        doc = repo.documents_brief(conn, [occ["document_id"]]).get(occ["document_id"]) or {}
        where = locator_label(doc.get("locator_kind", "page"), occ["page_number"])
        header = (
            f"图片 {occ_id}(图号:{occ['figure_number'] or '无'},"
            f"位置:{where})\n图注:{occ['caption'] or '无'}"
        )
        return [
            {"type": "text", "text": header},
            {"type": "image", "base64": base64.b64encode(image_bytes).decode(),
             "mime_type": asset["mime_type"]},
        ]

    # ---- 回答后端的图片组装 ----

    def build_image_entries(self, conn: Connection, ctx: AuthContext, occ_ids: list[str],
                            dedupe_assets: bool = True) -> list[dict]:
        """构造图片条目。dedupe_assets=True(默认)按图片资产去重,用于自动补图;
        正文显式引用路径传 False,让每个有效锚点都保留自己的图号与出处页。

        source_page_url 仅当文档有页预览(当前仅 pdf)时给出,否则 None;
        locator_label 为界面统一的位置文案(§3.9-4)。
        """
        entries: list[dict] = []
        seen_assets: set[str] = set()
        docs: dict[str, dict] = {}
        for occ_id in occ_ids:
            occ = repo.get_occurrence(conn, occ_id)
            if occ is None or occ["tenant_id"] != ctx.tenant_id:
                continue    # 丢弃无效/越权引用(§6)
            if dedupe_assets:
                if occ["image_asset_id"] in seen_assets:
                    continue    # 同图多处出现时按命中上下文取第一个正确出处
                seen_assets.add(occ["image_asset_id"])
            doc_id = occ["document_id"]
            if doc_id not in docs:
                docs.update(repo.documents_brief(conn, [doc_id]))
            doc = docs.get(doc_id) or {}
            kind = doc.get("locator_kind", "page")
            has_preview = doc.get("source_format") == "pdf"
            entries.append({
                "image_occurrence_id": occ_id,
                "document_id": doc_id,
                "figure_number": occ["figure_number"],
                "caption": occ["caption"],
                "page": occ["page_number"],
                "locator_label": locator_label(kind, occ["page_number"]),
                "image_url": f"/api/documents/{doc_id}/images/{occ_id}",
                "source_page_url": f"/api/documents/{doc_id}/pages/{occ['page_number']}" if has_preview else None,
            })
        return entries
