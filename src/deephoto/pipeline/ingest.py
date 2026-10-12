"""入库编排:queued → parsing → describing → indexing → ready(开发文档§3.1)。

确定性后台任务;所有写入以稳定键幂等,可重复执行。
同租户+同文件哈希+同解析版本的上传,克隆已有解析结果(§3.1.2)。
"""

from __future__ import annotations

import io
import logging
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

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

# 重试提示(DEV_describe_parse_retry §4.4):追加在原提示词与上下文之后,不改动 _PROMPT 本体;
# {reason} 为该项上一轮的机读失败原因(field_type 类原因本身已点名出错字段)
_RETRY_HINT = ("上一次输出无法解析为 JSON(原因:{reason})。请只输出一个紧凑的 JSON 对象,"
               "不要使用 Markdown 代码块,不要在 JSON 前后写任何文字;visible_summary 控制在 300 字以内,"
               "表格只概括结构与关键列,不要逐行抄录。")


def _call_model(model, image_bytes, mime_type, caption, section, context_text, retry_hint=None):
    """工作线程唯一职责:调用描述模型。纯函数,不碰数据库/observer/共享可变状态(§3.1)。

    describe_image 在调用时从模块全局解析,测试的模块级 patch 对本函数同样生效。
    """
    return describe_image(model, image_bytes, mime_type,
                          caption=caption, section=section, context_text=context_text,
                          retry_hint=retry_hint)


def _looks_rate_limited(exc: Exception) -> bool:
    """疑似限流:status_code == 429 或异常类名含 RateLimit。
    getattr 探测,不引入对 openai 的硬依赖。"""
    if getattr(exc, "status_code", None) == 429:
        return True
    return "RateLimit" in type(exc).__name__


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
                 mineru_client_factory=None, retry_model_factory=None, sleep=None):
        self.settings = settings
        self.store = store
        self.parser = parser
        self.index_service = index_service
        self._chat_model_factory = chat_model_factory   # 注:在 IngestService 内代表"描述模型"工厂
        self._chat_model = None
        self._progress_store = progress_store            # 观测存储;None 时全部走 NoOpObserver
        self._mineru_client_factory = mineru_client_factory  # 测试注入假客户端(on_progress) -> client
        # parse_failed 集中重试(DEV_describe_parse_retry §4.3/§4.4)的装配参数:
        # retry_model_factory(max_tokens)->按输出上限另建的模型;sleep 为轮间退避,测试注入空函数
        self._retry_model_factory = retry_model_factory
        self._sleep = sleep or time.sleep
        self._retry_models = {}   # 按 max_tokens 缓存重试模型(同一进程内一个值只建一次,§4.4)

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
        retry_counts = (stages.get(pg.STAGE_DESCRIBE_RETRY) or {}).get("counts")
        if retry_counts:
            # 描述重试汇总(DEV_describe_parse_retry §4.7.5):子阶段存在才输出
            logger.info("描述重试 doc=%s: 共 %s 项,恢复 %s 项,仍失败 %s 项",
                        doc["id"], retry_counts.get("total"), retry_counts.get("recovered"),
                        retry_counts.get("still_failed"))

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
        # unit:非 PDF 的"页"可能是幻灯片/虚拟分段,前端按 locator_kind 显示单位,不再一律写"页"
        observer.stage_end(pg.STAGE_PARSING,
                           counts={"pages": parsed.page_count, "unit": parsed.locator_kind})

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
            # 结果按最终统计计算:重试恢复后 failed 归零即 succeeded(§4.7.4)
            result = pg.RESULT_SUCCEEDED if not stats["failed"] else pg.RESULT_PARTIAL
            # 重试单项的 item_end 会同步推进 describing 的 counts(观测层同步所有在途阶段),
            # 跑过重试时按首轮口径修正,避免 completed 超过 total 露给前端(§4.7.1)
            counts = ({"completed": len(occurrences), "total": len(occurrences),
                       "succeeded": stats["ok"], "failed": stats["failed"], "degraded": 0}
                      if stats.get("retry") else None)
            observer.stage_end(pg.STAGE_DESCRIBING, result, counts=counts)

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
        """首轮逐张描述并记录进度;随后对 parse_failed 项集中重试(DEV_describe_parse_retry)。

        返回统计 {model, ok, failed, rate_limited, parse_failed_items[, retry]}
        (failed 含异常与格式失败;retry 键仅在跑了重试子阶段时出现)。
        并发度 1 走原同步循环(顺序、进度、结果与旧版完全一致);>1 走滑动窗口:
        工作线程只调 describe_image(纯函数),读库/写库/观测全部留在本线程(§3.1)。
        """
        model = self._get_chat_model()
        stats = {"model": model, "ok": 0, "failed": 0, "rate_limited": 0, "parse_failed_items": []}
        if model is None:
            logger.warning("图片描述已关闭,跳过(仅以图注检索)")
            return stats
        desc_model_tag = f"{self.settings.description_model}:{DESC_PROMPT_VERSION}"
        work = [(seq, occ, None) for seq, occ in enumerate(occurrences, start=1)]   # 首轮无重试提示

        def on_result(conn, occ, seq, label, result):
            self._settle_description(conn, occ, seq, label, result, desc_model_tag,
                                     observer, stats)

        def on_error(conn, occ, seq, label, exc):
            self._fail_description(conn, occ, seq, label, exc, desc_model_tag,
                                   observer, stats)

        self._dispatch_describe(conn, parsed, work, chunks, observer,
                                kind="image", model=model,
                                on_result=on_result, on_error=on_error)
        # 首轮全部结束后才集中重试(§4.1):重试不占用首轮并发窗口,且完成于 indexing 之前
        self._retry_parse_failed(conn, parsed, chunks, observer, stats, desc_model_tag)
        if stats["rate_limited"]:
            observer.warn(f"图片描述有 {stats['rate_limited']} 张疑似被服务商限流,"
                          "可调低 DEEPHOTO_DESCRIPTION_CONCURRENCY")
        return stats

    def _dispatch_describe(self, conn, parsed, work, chunks, observer, *,
                           kind, model, on_result, on_error) -> None:
        """首轮与重试轮共用的调度入口:work 为 (seq, occ, retry_hint) 清单(§6 调度参数化)。

        kind 为观测单项类型("image" / "image_retry");并发 >1 且待处理多于 1 项时走
        滑动窗口,否则顺序执行;落库回调由调用方按轮次提供。
        """
        if self.settings.description_concurrency > 1 and len(work) > 1:
            self._describe_concurrent(conn, parsed, work, chunks, observer,
                                      kind=kind, model=model,
                                      on_result=on_result, on_error=on_error)
        else:
            self._describe_sequential(conn, parsed, work, chunks, observer,
                                      kind=kind, model=model,
                                      on_result=on_result, on_error=on_error)

    def _describe_sequential(self, conn, parsed, work, chunks, observer, *,
                           kind, model, on_result, on_error) -> None:
        for seq, occ, hint in work:
            full = repo.get_occurrence(conn, occ["id"])
            asset = repo.get_asset(conn, full["image_asset_id"])
            label = _display_label(occ) or f"第 {seq} 张"
            observer.item_start(kind, seq, label=label,
                                page=occ["page_number"], figure=occ["figure_number"])
            try:
                image_bytes = self.store.get(asset["original_object_key"])
                result = describe_image(
                    model, image_bytes, asset["mime_type"],
                    caption=occ["caption"],
                    section=_section_for_page(parsed, occ["page_number"]),
                    context_text=_context_for_occurrence(occ, chunks, parsed),
                    retry_hint=hint,
                )
                on_result(conn, occ, seq, label, result)
            except Exception as exc:
                on_error(conn, occ, seq, label, exc)

    def _describe_concurrent(self, conn, parsed, work, chunks, observer, *,
                             kind, model, on_result, on_error) -> None:
        """滑动窗口:任一时刻至多 N 个在途调用。

        item_start 在提交时(而非排队时)调用,duration_ms 是单次调用真实耗时,
        不含排队;读图与上下文计算在提交前做,失败按单图失败处理,不占窗口(§3.6)。
        """
        limit = self.settings.description_concurrency
        pending = deque(work)
        inflight: dict = {}          # future -> (seq, occ, label)
        executor = ThreadPoolExecutor(max_workers=limit)

        def refresh_current_label() -> None:
            """列表行"当前"只有一格:在途多于一张时显示聚合文案,收尾恢复单张标签(§7-B)。"""
            if not inflight:
                return
            seq, occ, label = min(inflight.values())   # 最久在途:已等待时间按它算
            if len(inflight) > 1:
                observer.item_update(kind, seq, label=f"{len(inflight)} 张并发处理中")
            else:
                observer.item_update(kind, seq, label=label)

        def submit_one() -> None:
            seq, occ, hint = pending.popleft()
            full = repo.get_occurrence(conn, occ["id"])
            asset = repo.get_asset(conn, full["image_asset_id"])
            label = _display_label(occ) or f"第 {seq} 张"
            observer.item_start(kind, seq, label=label,
                                page=occ["page_number"], figure=occ["figure_number"])
            try:
                # 与串行路径同一 try 口径(§3.6):读图与上下文计算的失败都只算单图失败
                image_bytes = self.store.get(asset["original_object_key"])
                section = _section_for_page(parsed, occ["page_number"])
                context_text = _context_for_occurrence(occ, chunks, parsed)
            except Exception as exc:
                on_error(conn, occ, seq, label, exc)
                return
            future = executor.submit(
                _call_model, model, image_bytes, asset["mime_type"],
                occ["caption"], section, context_text, hint)
            inflight[future] = (seq, occ, label)
            refresh_current_label()

        try:
            while pending or inflight:
                while pending and len(inflight) < limit:
                    submit_one()
                if not inflight:
                    break                # 读图全部失败:无任务可等
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for future in done:
                    seq, occ, label = inflight.pop(future)
                    exc = future.exception()
                    if exc is not None:
                        on_error(conn, occ, seq, label, exc)
                    else:
                        try:
                            # 与串行路径同一 try 口径(§3.6):settle 出错也只算单图失败
                            on_result(conn, occ, seq, label, future.result())
                        except Exception as settle_exc:
                            on_error(conn, occ, seq, label, settle_exc)
                refresh_current_label()
        finally:
            # 主线程异常上抛时:取消未开始的任务;已开始的最多再跑一个超时周期
            executor.shutdown(wait=False, cancel_futures=True)

    def _settle_description(self, conn, occ, seq, label, result, desc_model_tag,
                            observer, stats) -> None:
        """主线程:首轮成功调用的落库与观测(两条路径共用)。"""
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
            return
        # 调用完成但输出不是有效 JSON:按格式失败计数,不能算成功
        stats["failed"] += 1
        observer.item_end("image", seq, pg.ITEM_PARSE_FAILED)
        if self.settings.description_parse_retry_rounds > 0:
            # 重试开启:登记进集中重试清单,首轮暂不逐条 warn;
            # 重试全部结束后仍失败才统一告警(§4.7.3)
            diagnostic = result.get("diagnostic") or {}
            item = {
                "occ": occ, "seq": seq, "label": label,
                "reason": diagnostic.get("reason") or "输出不是有效 JSON",
                "code": diagnostic.get("code"),
                "truncated": bool(diagnostic.get("truncated")),
                "max_tokens": self.settings.description_max_tokens,   # 升级起点:首轮上限
            }
            # 首轮诊断已决定第 1 个重试轮的目标上限(截断翻倍,§4.4)
            self._raise_retry_target(item)
            stats["parse_failed_items"].append(item)
        else:
            # 重试关闭:保持旧行为,首轮立即 warn(§4.7.3)
            observer.warn(f"第 {seq} 张({label})描述格式解析失败,已降级")

    def _fail_description(self, conn, occ, seq, label, exc, desc_model_tag,
                          observer, stats) -> None:
        """主线程:单图失败(读图/调用异常)不阻塞整篇入库(§6:检索命中后看原图兜底)。"""
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
        if _looks_rate_limited(exc):
            stats["rate_limited"] += 1
        observer.item_end("image", seq, pg.ITEM_ERROR, error_kind=type(exc).__name__)

    # ---- parse_failed 集中重试(DEV_describe_parse_retry §4)----

    def _retry_parse_failed(self, conn, parsed, chunks, observer, stats, desc_model_tag) -> None:
        """首轮结束后对 parse_failed 项集中重试;全部完成于 indexing 之前。

        每轮按清单内最大目标 max_tokens 构造一个共用模型,逐项带原因提示(§4.4);
        恢复的项覆盖描述并移出清单。模型/配置异常只放弃重试,绝不使文档 failed(§4.6/§7)。
        """
        items = stats["parse_failed_items"]
        rounds = self.settings.description_parse_retry_rounds
        if not items or rounds <= 0:
            return
        total = len(items)
        # 子阶段不带 total:counts 由 stage_end 按 {total, recovered, still_failed, rounds}
        # 自定义(§4.7.1),避免重试单项的 item_end 把子阶段也同步成首轮计数口径
        observer.stage_start(pg.STAGE_DESCRIBE_RETRY, parent=pg.STAGE_DESCRIBING,
                             detail={"rounds": rounds})
        rounds_run = 0
        try:
            for round_no in range(1, rounds + 1):
                if not items:
                    break
                if round_no > 1:
                    self._sleep(self.settings.description_retry_backoff_seconds)   # §4.3 轮间退避
                rounds_run = round_no
                model, round_tokens = self._retry_model_for_round(items, stats["model"])
                retry_tag = f"{desc_model_tag}+r{round_no}"      # 恢复项可追溯(§4.6)
                by_id = {it["occ"]["id"]: it for it in items}
                recovered: list[str] = []

                def on_result(conn, occ, seq, label, result,
                              _r=round_no, _mt=round_tokens, _tag=retry_tag):
                    if self._settle_retry(conn, by_id[occ["id"]], result, _tag,
                                          observer, stats, _r, _mt):
                        recovered.append(occ["id"])

                def on_error(conn, occ, seq, label, exc,
                             _r=round_no, _mt=round_tokens):
                    self._fail_retry(conn, by_id[occ["id"]], exc, observer, stats, _r, _mt)

                work = [(it["seq"], it["occ"], _RETRY_HINT.format(reason=it["reason"]))
                        for it in items]
                self._dispatch_describe(conn, parsed, work, chunks, observer,
                                        kind="image_retry", model=model,
                                        on_result=on_result, on_error=on_error)
                items = [it for it in items if it["occ"]["id"] not in recovered]
        except Exception as exc:
            # 轮级异常(含 retry_model_factory 抛错):告警并放弃后续轮次,不得使文档 failed(§7)
            summary = error_summary(exc, self._secret_values())
            logger.error("describe retry aborted: %s", summary)
            logger.debug("describe retry abort traceback", exc_info=True)
            observer.warn(f"描述重试中断({summary}),剩余 {len(items)} 项保持降级")
        # 收尾:仍失败项保留首轮降级记录,仅 uncertain_details 追加留痕并统一 warn(§4.1/§4.7.3)
        for it in items:
            self._mark_retry_exhausted(conn, it, rounds_run, desc_model_tag)
            observer.warn(f"第 {it['seq']} 张({it['label']})描述格式解析失败,"
                          f"已重试 {rounds_run} 次,已降级")
        stats["retry"] = {"total": total, "recovered": total - len(items),
                          "still_failed": len(items), "rounds": rounds_run}
        observer.stage_end(pg.STAGE_DESCRIBE_RETRY,
                           pg.RESULT_SUCCEEDED if not items else pg.RESULT_PARTIAL,
                           counts=stats["retry"])

    def _retry_model_for_round(self, items, base_model):
        """本轮共用模型与输出上限:取清单内最大目标 max_tokens,整轮共用不做逐项差异化;
        未装配 retry_model_factory 时复用首轮模型(只有提示词变化,§4.4)。"""
        target = max(it["max_tokens"] for it in items)
        if self._retry_model_factory is None:
            return base_model, target
        if target not in self._retry_models:
            self._retry_models[target] = self._retry_model_factory(target)   # 异常由轮级捕获(§7)
        return self._retry_models[target], target

    def _settle_retry(self, conn, item, result, desc_model_tag, observer, stats,
                      round_no, max_tokens) -> bool:
        """重试轮成功调用的落库与观测;返回是否恢复。仍 parse_failed 不写库,留待下一轮(§4.6)。"""
        occ, seq = item["occ"], item["seq"]
        diag = result.get("diagnostic") or {}
        detail = {"round": round_no, "max_tokens": max_tokens}
        if diag.get("result", "ok") == "ok":
            repo.update_occurrence_description(
                conn, occ["id"], visible_summary=result["visible_summary"],
                visible_labels=result["visible_labels"],
                caption=result["caption"] or (occ["caption"] or ""),
                context_summary=result["context_summary"],
                uncertain_details=result["uncertain_details"],
                description_model=desc_model_tag,
            )
            stats["ok"] += 1
            stats["failed"] -= 1            # 恢复一项,抵消首轮的格式失败计数(§4.7.4)
            observer.item_end("image_retry", seq, pg.ITEM_OK, detail=detail)
            return True
        # 仍失败:更新机读原因供下一轮决定 max_tokens 与提示;不覆盖首轮降级记录
        item["reason"] = diag.get("reason") or item["reason"]
        item["code"] = diag.get("code")
        item["truncated"] = bool(diag.get("truncated"))
        self._raise_retry_target(item)
        observer.item_end("image_retry", seq, pg.ITEM_PARSE_FAILED, detail=detail)
        return False

    def _fail_retry(self, conn, item, exc, observer, stats, round_no, max_tokens) -> bool:
        """重试轮异常:视为该项本轮失败,进入下一轮;限流沿用现有口径(§4.6/§7)。绝不向上抛。"""
        summary = error_summary(exc, self._secret_values())
        logger.error("describe retry failed for %s: %s", item["occ"]["id"], summary)
        logger.debug("describe retry failure traceback", exc_info=True)
        item["reason"] = f"描述生成失败:{summary}"      # 供重试用尽时留痕
        item["code"] = None
        item["truncated"] = False                       # 异常不带截断信号,目标不升级
        if _looks_rate_limited(exc):
            stats["rate_limited"] += 1
        observer.item_end("image_retry", item["seq"], pg.ITEM_ERROR,
                          error_kind=type(exc).__name__,
                          detail={"round": round_no, "max_tokens": max_tokens})
        return False

    def _raise_retry_target(self, item) -> None:
        """下一轮目标 max_tokens:截断(含保守判定的 invalid_json)翻倍并封顶 CAP;其余不变(§4.4)。"""
        if item["truncated"] or item["code"] == "invalid_json":
            item["max_tokens"] = min(item["max_tokens"] * 2,
                                     self.settings.description_retry_max_tokens_cap)

    def _mark_retry_exhausted(self, conn, item, rounds_run, desc_model_tag) -> None:
        """重试用尽:保留首轮降级内容,仅 uncertain_details 追加留痕(§4.6)。幂等覆盖写。"""
        cur = repo.get_occurrence(conn, item["occ"]["id"])
        note = f"已重试 {rounds_run} 次仍失败:{item['reason']}"
        repo.update_occurrence_description(
            conn, item["occ"]["id"],
            visible_summary=cur["description"], visible_labels=cur["visible_labels"],
            caption=cur["caption"] or "", context_summary=cur["context_summary"],
            uncertain_details=(cur["uncertain_details"] or []) + [note],
            description_model=cur["description_model"] or desc_model_tag,
        )

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
