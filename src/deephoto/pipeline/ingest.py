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
from ..parsing.base import (
    KIND_PAGE_FALLBACK,
    LayoutParser,
    ParsedDocument,
)
from ..parsing.formats import format_by_key
from ..parsing.registry import SourceFile, create_parser, validate_parsed
from ..sanitize import error_summary
from ..storage import ObjectStore
from .chunking import chunk_paragraphs
from .describe import DESC_PROMPT_VERSION, describe_image
from .linking import resolve_links, whole_document_links
from .progress import NoOpObserver
from . import progress as pg

logger = logging.getLogger(__name__)

_MIME_BY_PIL_FORMAT = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def _display_label(occ: dict) -> str | None:
    """图号/表号展示名(给用户看);内部 occurrence ID 只进诊断,不进入口。"""
    number = occ.get("figure_number")
    if not number:
        return None
    if str(number).startswith("表"):
        return f"表 {str(number)[1:]}"
    return f"图 {number}"


class IngestService:
    def __init__(self, settings: Settings, store: ObjectStore, parser: LayoutParser,
                 index_service, chat_model_factory=None, progress_store=None,
                 mineru_client_factory=None):
        self.settings = settings
        self.store = store
        self.parser = parser
        self.index_service = index_service
        self._chat_model_factory = chat_model_factory   # 注:在 IngestService 内代表"描述模型"工厂
        self._chat_model = None
        self._progress_store = progress_store            # 观测存储;None 时全部走 NoOpObserver
        self._mineru_client_factory = mineru_client_factory  # 测试注入假客户端(on_progress) -> client

    def ingest(self, document_id: str) -> None:
        conn = connect(self.settings.db_path)
        doc = repo.get_document(conn, document_id)
        if doc is None or doc["status"] not in ("queued", "failed"):
            return
        observer = self._observer_for(document_id)
        try:
            self._run(conn, doc, observer)
            observer.finish(pg.RESULT_SUCCEEDED)
            self._log_run_summary(doc)
        except Exception as exc:  # 失败可重试:状态落库,worker 不中断
            summary = error_summary(exc, self._secret_values(), max_chars=300)
            logger.error("ingest failed for %s: %s", document_id, summary)
            logger.debug("ingest failure traceback", exc_info=True)
            observer.finish(pg.RESULT_FAILED)
            repo.update_document_status(conn, document_id, "failed", error=summary)

    def _observer_for(self, document_id: str):
        """绑定该文档最新运行的观察器;无观测存储或无登记记录(旧文档)时用空实现。"""
        if self._progress_store is None:
            return NoOpObserver()
        run_id = self._progress_store.latest_run_id(document_id)
        return self._progress_store.observer(run_id) if run_id else NoOpObserver()

    def _log_run_summary(self, doc: dict) -> None:
        """任务结束输出一次简短汇总(父子阶段不重复求和)。"""
        if self._progress_store is None:
            return
        detail = self._progress_store.detail(doc["tenant_id"], doc["id"], doc["status"])
        if not detail:
            return
        stages = {s["stage"]: s for s in detail["stages"]}

        def ms(name: str) -> str:
            value = (stages.get(name) or {}).get("duration_ms")
            return f"{value / 1000:.1f}s" if value is not None else "未知"

        slow = detail["slowest"][:1]
        slow_txt = (f", 最慢单项「{slow[0]['label'] or slow[0]['kind']}」"
                    f" {slow[0]['duration_ms'] / 1000:.1f}s") if slow else ""
        logger.info(
            "入库耗时汇总 doc=%s: 排队=%s 解析=%s 描述=%s 索引=%s%s,警告 %d 条",
            doc["id"], ms(pg.STAGE_QUEUED), ms(pg.STAGE_PARSING), ms(pg.STAGE_DESCRIBING),
            ms(pg.STAGE_INDEXING), slow_txt, len(detail["summary"]["warnings"]))

    # ---- 内部 ----

    def _run(self, conn, doc: dict, observer) -> None:
        document_id = doc["id"]
        tenant_id = doc["tenant_id"]
        version = doc["ingestion_version"]

        # 1) 去重复用:同租户已有同内容同版本的 ready 文档 -> 直接克隆
        observer.stage_start(pg.STAGE_DEDUP)
        sibling = repo.find_ready_document_by_hash(conn, tenant_id, doc["sha256"], version,
                                                   doc.get("parse_engine"))
        hit = sibling is not None and sibling["id"] != document_id
        observer.stage_end(pg.STAGE_DEDUP, detail={"hit": hit})
        if hit:
            observer.stage_start(pg.STAGE_REUSE)
            chunk_map, occ_map = repo.clone_document_data(conn, src_document_id=sibling["id"], dst=doc)
            self.index_service.clone_document(conn, src_document_id=sibling["id"], dst=doc,
                                              chunk_id_map=chunk_map, occ_id_map=occ_map)
            repo.update_document_status(conn, document_id, "ready", page_count=sibling["page_count"])
            conn.commit()
            observer.stage_end(pg.STAGE_REUSE)
            return

        # 2) 解析:按文档记录的格式与引擎分发(PDF→MinerU;md/txt→本地;见 parsing/registry)
        observer.stage_start(pg.STAGE_PARSING, detail={
            "engine": doc.get("parse_engine"), "format": doc.get("source_format")})
        repo.update_document_status(conn, document_id, "parsing")
        source_bytes = self.store.get(doc["source_object_key"])
        parsed = self._parse(doc, source_bytes, observer)
        observer.stage_end(pg.STAGE_PARSING, counts={"pages": parsed.page_count})

        observer.stage_start(pg.STAGE_FIGURES)
        occurrences = self._persist_figures(
            conn, doc, parsed, source_bytes if doc.get("source_format") == "pdf" else None, observer)
        observer.stage_end(pg.STAGE_FIGURES, counts={"figures": len(occurrences)})

        observer.stage_start(pg.STAGE_CHUNKS)
        chunks = self._persist_chunks_and_links(conn, doc, parsed, occurrences)
        conn.commit()
        observer.stage_end(pg.STAGE_CHUNKS, counts={"chunks": len(chunks)})

        # 3) 图片描述(描述模型多模态;未启用时跳过,仅以图注检索)
        repo.update_document_status(conn, document_id, "describing")
        observer.stage_start(pg.STAGE_DESCRIBING, total=len(occurrences),
                             detail={"retry_note": "单次调用计时含 SDK 内部重试(已观测次数未知)"})
        stats = self._describe_all(conn, doc, parsed, occurrences, chunks, observer)
        conn.commit()
        if stats["model"] is None:
            observer.stage_end(pg.STAGE_DESCRIBING, pg.RESULT_SKIPPED,
                               detail={"reason": "图片描述已关闭(DEEPHOTO_DESCRIPTION_ENABLED 未开启)"})
        else:
            result = pg.RESULT_SUCCEEDED if not stats["failed"] else pg.RESULT_PARTIAL
            observer.stage_end(pg.STAGE_DESCRIBING, result)

        # 4) 索引
        repo.update_document_status(conn, document_id, "indexing")
        observer.stage_start(pg.STAGE_INDEXING)
        self.index_service.upsert_document(conn, document_id, observer=observer)
        observer.stage_end(pg.STAGE_INDEXING)

        observer.stage_start(pg.STAGE_FINALIZING)
        repo.update_document_status(conn, document_id, "ready", page_count=parsed.page_count)
        conn.commit()
        observer.stage_end(pg.STAGE_FINALIZING)

    def _parse(self, doc: dict, data: bytes, observer) -> ParsedDocument:
        """按文档记录的格式与引擎分发解析;结果统一过不变量校验(不静默 ready 空文档)。

        解析器失败按异常上抛,由 ingest() 标记文档 failed 供重试。
        """
        fmt = format_by_key(doc["source_format"])
        parser = create_parser(fmt, self.settings, engine=doc.get("parse_engine"),
                               mineru_client_factory=self._mineru_client_factory)
        parsed = parser.parse(
            SourceFile(data=data, filename=doc.get("filename") or f"document.{fmt.ext}", fmt=fmt),
            observer)
        validate_parsed(parsed)
        return parsed

    def _persist_figures(self, conn, doc: dict, parsed: ParsedDocument,
                         source_bytes: bytes | None, observer) -> list[dict]:
        """保存图片资产与出现位置;返回 [{id, page_number, figure_number, caption, parsed_figure}]。

        source_bytes 仅 PDF 传入(用于整页/区域回退渲染);非 PDF 没有可渲染的源,
        既无 image_bytes 又无法回退的 figure 跳过并告警,不抛。
        """
        from PIL import Image

        is_pdf = doc.get("source_format") == "pdf" and source_bytes is not None
        records: list[dict] = []
        for page in parsed.pages:
            captions_by_id = {c.id: c for c in page.captions}
            for figure in page.figures:
                if figure.image_bytes:            # 嵌入位图,或解析器(MinerU/本地)提供的图
                    image_bytes = figure.image_bytes
                elif not is_pdf:
                    # 非 PDF 没有可渲染的源:跳过并告警,不能抛(§3.8)
                    logger.warning("figure %s has no bytes and no renderable source, skipped", figure.id)
                    observer.warn(f"第 {page.page_number} 个位置的一张图片没有可取的字节,已跳过")
                    continue
                elif figure.kind == KIND_PAGE_FALLBACK:
                    image_bytes = self.parser.render_page(source_bytes, page.page_number)
                else:
                    image_bytes = self.parser.render_region(source_bytes, page.page_number, figure.bbox)
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
                    observer.warn(f"一张图片格式无法识别(第 {page.page_number} 个位置),已跳过")
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
                    bbox_coord="pdf_points" if is_pdf else "none",
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
        links = resolve_links(chunks, occurrences)
        if doc.get("source_format") == "image":
            links += whole_document_links(chunks, occurrences)   # 图即全文(§3.4f)
        for link in links:
            repo.insert_link(conn, tenant_id=doc["tenant_id"], document_id=doc["id"],
                             chunk_id=link.chunk_id, image_occurrence_id=link.image_occurrence_id,
                             relation=link.relation, confidence=link.confidence)
        return chunks

    def _describe_all(self, conn, doc: dict, parsed: ParsedDocument, occurrences: list[dict],
                      chunks: list[dict], observer) -> dict:
        """逐张描述并记录进度;返回统计 {model, ok, failed}(failed 含异常与格式失败)。"""
        model = self._get_chat_model()
        stats = {"model": model, "ok": 0, "failed": 0}
        if model is None:
            logger.warning("图片描述已关闭,跳过(仅以图注检索)")
            return stats
        desc_model_tag = f"{self.settings.description_model}:{DESC_PROMPT_VERSION}"
        for seq, occ in enumerate(occurrences, start=1):
            full = repo.get_occurrence(conn, occ["id"])
            asset = repo.get_asset(conn, full["image_asset_id"])
            label = _display_label(occ) or f"第 {seq} 张"
            observer.item_start("image", seq, label=label,
                                page=occ["page_number"], figure=occ["figure_number"])
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
                    description_model=desc_model_tag,
                )
                diag = (result.get("diagnostic") or {}).get("result", "ok")
                if diag == "ok":
                    stats["ok"] += 1
                    observer.item_end("image", seq, pg.ITEM_OK)
                else:
                    # 调用完成但输出不是有效 JSON:按格式失败计数,不能算成功
                    stats["failed"] += 1
                    observer.item_end("image", seq, pg.ITEM_PARSE_FAILED)
                    observer.warn(f"第 {seq} 张({label})描述格式解析失败,已降级")
            except Exception as exc:
                # 单图失败不阻塞整篇入库(§6:描述漏掉关键内容 -> 检索命中后看原图兜底)
                summary = error_summary(exc, self._secret_values())
                logger.error("describe failed for %s: %s", occ["id"], summary)
                logger.debug("describe failure traceback", exc_info=True)
                repo.update_occurrence_description(
                    conn, occ["id"], visible_summary="", visible_labels=[],
                    caption=occ["caption"] or "", context_summary="",
                    uncertain_details=[f"描述生成失败:{summary}"],
                    description_model=desc_model_tag,
                )
                stats["failed"] += 1
                observer.item_end("image", seq, pg.ITEM_ERROR, error_kind=type(exc).__name__)
        return stats

    def _secret_values(self) -> list[str | None]:
        """脱敏用:所有已配置的密钥原值(任何一个出现在异常文本里都要抹掉)。"""
        st = self.settings
        return [st.description_api_key, st.moonshot_api_key,
                st.embedding_api_key, st.mineru_api_key]

    def _get_chat_model(self):
        """描述模型(进程内缓存)。工厂返回 None 表示"描述已关闭"(明确状态);
        初始化异常直接上抛,由 ingest() 记录文档失败——不伪装成未配置。"""
        if self._chat_model is None and self._chat_model_factory is not None:
            self._chat_model = self._chat_model_factory()
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
