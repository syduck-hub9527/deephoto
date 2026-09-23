"""入库编排:queued → parsing → describing → indexing → ready(开发文档§3.1)。

确定性后台任务;所有写入以稳定键幂等,可重复执行。
同租户+同文件哈希+同解析版本的上传,克隆已有解析结果(§3.1.2)。
"""

from __future__ import annotations

import io
import logging

from .. import repo
from ..config import Settings
from ..db import connect
from ..parsing.base import KIND_EMBEDDED_BITMAP, KIND_PAGE_FALLBACK, LayoutParser, ParsedDocument
from ..storage import ObjectStore
from .chunking import chunk_paragraphs
from .describe import DESC_PROMPT_VERSION, describe_image
from .linking import resolve_links

logger = logging.getLogger(__name__)

_MIME_BY_PIL_FORMAT = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class IngestService:
    def __init__(self, settings: Settings, store: ObjectStore, parser: LayoutParser,
                 index_service, chat_model_factory=None):
        self.settings = settings
        self.store = store
        self.parser = parser
        self.index_service = index_service
        self._chat_model_factory = chat_model_factory
        self._chat_model = None

    def ingest(self, document_id: str) -> None:
        conn = connect(self.settings.db_path)
        doc = repo.get_document(conn, document_id)
        if doc is None or doc["status"] not in ("queued", "failed"):
            return
        try:
            self._run(conn, doc)
        except Exception as exc:  # 失败可重试:状态落库,worker 不中断
            logger.exception("ingest failed for %s", document_id)
            repo.update_document_status(conn, document_id, "failed", error=f"{type(exc).__name__}: {exc}")

    # ---- 内部 ----

    def _run(self, conn, doc: dict) -> None:
        document_id = doc["id"]
        tenant_id = doc["tenant_id"]
        version = doc["ingestion_version"]

        # 1) 去重复用:同租户已有同内容同版本的 ready 文档 -> 直接克隆
        sibling = repo.find_ready_document_by_hash(conn, tenant_id, doc["sha256"], version)
        if sibling is not None and sibling["id"] != document_id:
            chunk_map, occ_map = repo.clone_document_data(conn, src_document_id=sibling["id"], dst=doc)
            self.index_service.clone_document(conn, src_document_id=sibling["id"], dst=doc,
                                              chunk_id_map=chunk_map, occ_id_map=occ_map)
            repo.update_document_status(conn, document_id, "ready", page_count=sibling["page_count"])
            conn.commit()
            return

        # 2) 解析
        repo.update_document_status(conn, document_id, "parsing")
        pdf_bytes = self.store.get(doc["pdf_object_key"])
        parsed = self.parser.parse(pdf_bytes)
        occurrences = self._persist_figures(conn, doc, parsed, pdf_bytes)
        chunks = self._persist_chunks_and_links(conn, doc, parsed, occurrences)
        conn.commit()

        # 3) 图片描述(K3 多模态;无 API key 时跳过,仅以图注检索)
        repo.update_document_status(conn, document_id, "describing")
        self._describe_all(conn, doc, parsed, occurrences, chunks)
        conn.commit()

        # 4) 索引
        repo.update_document_status(conn, document_id, "indexing")
        self.index_service.upsert_document(conn, document_id)
        repo.update_document_status(conn, document_id, "ready", page_count=parsed.page_count)
        conn.commit()

    def _persist_figures(self, conn, doc: dict, parsed: ParsedDocument, pdf_bytes: bytes) -> list[dict]:
        """保存图片资产与出现位置;返回 [{id, page_number, figure_number, caption, parsed_figure}]。"""
        from PIL import Image

        records: list[dict] = []
        for page in parsed.pages:
            captions_by_id = {c.id: c for c in page.captions}
            for figure in page.figures:
                if figure.kind == KIND_EMBEDDED_BITMAP and figure.image_bytes:
                    image_bytes = figure.image_bytes
                elif figure.kind == KIND_PAGE_FALLBACK:
                    image_bytes = self.parser.render_page(pdf_bytes, page.page_number)
                else:
                    image_bytes = self.parser.render_region(pdf_bytes, page.page_number, figure.bbox)
                try:
                    with Image.open(io.BytesIO(image_bytes)) as im:
                        width, height = im.size
                        mime_type = _MIME_BY_PIL_FORMAT.get(im.format or "", "image/png")
                        if mime_type != "image/png" and im.format not in _MIME_BY_PIL_FORMAT:
                            image_bytes = _to_png(im)
                            mime_type = "image/png"
                except Exception:
                    # 无法识别的嵌入图:统一转 PNG 失败则跳过该图
                    logger.warning("unreadable figure image %s, skipped", figure.id)
                    continue
                object_key, digest = self.store.put(image_bytes, "images", mime_type)
                asset_id = repo.get_or_create_asset(
                    conn, tenant_id=doc["tenant_id"], sha256=digest, object_key=object_key,
                    width=width, height=height, mime_type=mime_type,
                )
                caption = captions_by_id.get(figure.caption_id or "")
                occ_id = repo.insert_occurrence(
                    conn, tenant_id=doc["tenant_id"], document_id=doc["id"],
                    ingestion_version=doc["ingestion_version"], image_asset_id=asset_id,
                    page_number=page.page_number, bbox=list(figure.bbox),
                    figure_number=caption.figure_number if caption else None,
                    caption=caption.text if caption else None,
                    extraction_method=figure.kind,
                    needs_review=figure.kind == KIND_PAGE_FALLBACK,
                )
                records.append({
                    "id": occ_id, "page_number": page.page_number,
                    "figure_number": caption.figure_number if caption else None,
                    "caption": caption.text if caption else None,
                })
        return records

    def _persist_chunks_and_links(self, conn, doc: dict, parsed: ParsedDocument,
                                  occurrences: list[dict]) -> list[dict]:
        figure_map = _figure_number_map(occurrences)
        chunks: list[dict] = []
        for draft in chunk_paragraphs(parsed.all_paragraphs()):
            referenced = [figure_map[n] for n in draft.referenced_figures if n in figure_map]
            nearby = [
                o["id"] for o in occurrences
                if draft.page_start <= o["page_number"] <= draft.page_end and o["id"] not in referenced
            ]
            chunk_id = repo.insert_chunk(
                conn, tenant_id=doc["tenant_id"], document_id=doc["id"],
                ingestion_version=doc["ingestion_version"], section=draft.section, text=draft.text,
                page_start=draft.page_start, page_end=draft.page_end,
                paragraph_ids=draft.paragraph_ids, referenced_image_ids=referenced,
                nearby_image_ids=nearby,
            )
            chunks.append({"id": chunk_id, "text": draft.text,
                           "page_start": draft.page_start, "page_end": draft.page_end,
                           "referenced_image_ids": referenced})
        for link in resolve_links(chunks, occurrences):
            repo.insert_link(conn, tenant_id=doc["tenant_id"], document_id=doc["id"],
                             chunk_id=link.chunk_id, image_occurrence_id=link.image_occurrence_id,
                             relation=link.relation, confidence=link.confidence)
        return chunks

    def _describe_all(self, conn, doc: dict, parsed: ParsedDocument, occurrences: list[dict],
                      chunks: list[dict]) -> None:
        model = self._get_chat_model()
        if model is None:
            logger.warning("未配置 DEEPHOTO_MOONSHOT_API_KEY,跳过图片描述(仅用图注检索)")
            return
        for occ in occurrences:
            full = repo.get_occurrence(conn, occ["id"])
            asset = repo.get_asset(conn, full["image_asset_id"])
            try:
                image_bytes = self.store.get(asset["original_object_key"])
                result = describe_image(
                    model, image_bytes, asset["mime_type"],
                    caption=occ["caption"],
                    section=_section_for_page(parsed, occ["page_number"]),
                    context_text=_context_for_occurrence(occ, chunks, parsed),
                )
                repo.update_occurrence_description(
                    conn, occ["id"], visible_summary=result["visible_summary"],
                    visible_labels=result["visible_labels"],
                    caption=result["caption"] or (occ["caption"] or ""),
                    context_summary=result["context_summary"],
                    uncertain_details=result["uncertain_details"],
                    description_model=f"{self.settings.chat_model}:{DESC_PROMPT_VERSION}",
                )
            except Exception as exc:
                # 单图失败不阻塞整篇入库(§6:描述漏掉关键内容 -> 检索命中后看原图兜底)
                logger.exception("describe failed for %s", occ["id"])
                repo.update_occurrence_description(
                    conn, occ["id"], visible_summary="", visible_labels=[],
                    caption=occ["caption"] or "", context_summary="",
                    uncertain_details=[f"描述生成失败:{type(exc).__name__}: {exc}"],
                    description_model=f"{self.settings.chat_model}:{DESC_PROMPT_VERSION}",
                )

    def _get_chat_model(self):
        if self._chat_model is None and self._chat_model_factory is not None:
            try:
                self._chat_model = self._chat_model_factory()
            except RuntimeError:
                return None
        return self._chat_model


def _to_png(image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _figure_number_map(occurrences: list[dict]) -> dict[str, str]:
    """图号 -> occurrence id(同号多现时取页码最小者)。"""
    result: dict[str, str] = {}
    for occ in sorted(occurrences, key=lambda o: o["page_number"]):
        if occ["figure_number"]:
            result.setdefault(occ["figure_number"], occ["id"])
    return result


def _section_for_page(parsed: ParsedDocument, page_number: int) -> str | None:
    for page in parsed.pages:
        if page.page_number == page_number:
            for para in page.paragraphs:
                if para.section:
                    return para.section
    return None


def _context_for_occurrence(occ: dict, chunks: list[dict], parsed: ParsedDocument) -> str:
    """优先取包含图注的块;否则取同页段落摘录。"""
    caption = (occ.get("caption") or "").strip()
    if caption:
        for chunk in chunks:
            if caption[:30] in chunk["text"]:
                return chunk["text"][:800]
    for page in parsed.pages:
        if page.page_number == occ["page_number"]:
            return "\n".join(p.text for p in page.paragraphs)[:800]
    return ""
