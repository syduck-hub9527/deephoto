"""MinerU 引擎的 DocumentParser 适配:把现有云端客户端包装成注册表协议。

PDF 路径行为与原 ingest._parse_with_mineru 一致(拆分/轮询/合并/按页兜底);
非 PDF 由 MinerUClient.parse_file 处理(不拆分,页数由元素推断)。
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..pipeline import progress as pg
from .base import ParsedDocument, ParsedPage, ParsedParagraph
from .content_list import PAGE_H, PAGE_W, build_document
from .formats import FormatInfo
from .mineru import MINERU_DEFAULT_BASE_URL, MinerUClient, MinerUError
from .registry import DocumentParser, SourceFile

logger = logging.getLogger(__name__)

# 云端已知状态翻译(仅确认含义的才翻;未知状态统一"等待云端结果",不制造百分比)
_CLOUD_STATE_LABEL = {"pending": "云端排队", "running": "云端解析"}


class _MinerUProgressAdapter:
    """把 MinerUClient 的普通事件翻译成观测器的阶段/单项记录。

    每块按 chunk_start → (申请/上传/轮询/下载) → extract_end 顺序记录;
    上传、等待、下载耗时分别放在单项 detail,不与 parsing 总耗时重复求和。
    """

    def __init__(self, observer):
        self._observer = observer
        self._download_ms: dict[int, int] = {}

    def __call__(self, event: dict) -> None:
        kind = event.get("type")
        index = event.get("index") or 1
        obs = self._observer
        if kind == "split":
            obs.stage_start("mineru_split", parent=pg.STAGE_PARSING)
            # 拆分/合并事件自带真实耗时(客户端已计时),不能让 stage_start/end 连记成 0
            obs.stage_end("mineru_split", duration_ms=event.get("duration_ms"), counts={
                "pages": event.get("pages"), "bytes": event.get("bytes"),
                "chunks": event.get("chunks")})
        elif kind == "chunk_start":
            pages = event.get("pages")
            label = f"第 {index} 部分({pages} 页)" if pages else f"第 {index} 部分"
            obs.item_start("mineru_chunk", index, label=label)
        elif kind == "request_url_end":
            obs.item_update("mineru_chunk", index, detail={"request_ms": event.get("duration_ms")})
        elif kind == "upload_end":
            obs.item_update("mineru_chunk", index, detail={"upload_ms": event.get("duration_ms")})
        elif kind == "poll":
            state = str(event.get("state") or "")
            label = f"第 {index} 部分 · {_CLOUD_STATE_LABEL.get(state, '等待云端结果')}"
            obs.item_update("mineru_chunk", index, label=label, detail={
                "last_state": state, "polls": event.get("attempts"),
                "wait_ms": event.get("elapsed_ms")})
        elif kind == "download_end":
            self._download_ms[index] = event.get("duration_ms") or 0
        elif kind == "extract_end":
            detail = {
                "download_ms": self._download_ms.pop(index, None),
                "extract_ms": event.get("duration_ms"),
                "elements": event.get("elements"), "figures": event.get("figures"),
                "fallback": event.get("fallback"),
            }
            obs.item_end("mineru_chunk", index, pg.ITEM_OK, detail=detail)
        elif kind == "merge_end":
            obs.stage_start("mineru_merge", parent=pg.STAGE_PARSING)
            # 只含实际合并操作耗时(客户端逐块拼接时累计),用事件自带值
            obs.stage_end("mineru_merge", duration_ms=event.get("duration_ms"), counts={
                "chunks": event.get("chunks"), "pages": event.get("pages")})


class MinerUParser:
    """MinerU 云端引擎;engine='mineru'。无 Token 直接失败(现状,本地兜底见 P4)。"""

    engine = "mineru"

    def __init__(self, settings: Settings, client_factory=None):
        self._settings = settings
        self._client_factory = client_factory   # 测试注入:(on_progress) -> client

    def parse(self, src: SourceFile, observer) -> ParsedDocument:
        """整文件交 MinerU 解析;失败上抛,由入库标记文档 failed 供重试。

        不本地判扫描、不本地渲染页、不存整页图(600+页扫描版不再产生整页图)。
        """
        settings = self._settings
        if not settings.mineru_api_key:
            raise MinerUError("未配置 DEEPHOTO_MINERU_API_KEY,无法解析文档")
        adapter = _MinerUProgressAdapter(observer)
        if self._client_factory is not None:
            client = self._client_factory(adapter)
        else:
            client = MinerUClient(
                api_key=settings.mineru_api_key,
                base_url=settings.mineru_base_url or MINERU_DEFAULT_BASE_URL,
                dump_dir=settings.mineru_dump_dir,
                on_progress=adapter,
                ocr_disabled=settings.mineru_no_ocr_formats,
            )
        result = client.parse_file(src.data, src.filename, src.fmt)
        if result.elements:
            doc = build_document(result.elements, page_count=len(result.page_texts))
        else:
            logger.warning("MinerU 结果不含 content_list,退回按页纯文本(无图/表/标题)")
            observer.warn("MinerU 结果不含 content_list,已退回纯文本(无图/表)")
            doc = pages_from_texts(result.page_texts)
        if src.fmt.key == "image":
            _attach_original_figure(doc, src)
        return doc


def _attach_original_figure(doc: ParsedDocument, src: SourceFile) -> None:
    """图片格式:整图本身就是唯一配图,放到第 1 页(图片来源=原图本身,§2 矩阵)。

    MinerU 对图片输入通常只回 OCR 文本(样本实测无 image 元素);若个别输入
    (截图/图表类)返回了裁好的子图,也以原图为准——子图是原图的区域裁剪,
    并存会在 QA 里重复配图,故丢弃子图只留原图(子图的图注文本仍留在段落里,
    可检索)。
    """
    from io import BytesIO

    from PIL import Image

    from .base import KIND_EMBEDDED_BITMAP, ParsedFigure

    try:
        with Image.open(BytesIO(src.data)) as img:
            width, height = img.size
    except Exception:                # 魔数已过但解码失败:占位尺寸,字节照常入库
        width, height = int(PAGE_W), int(PAGE_H)
    for page in doc.pages:
        page.figures.clear()         # 丢弃 MinerU 可能返回的子图裁剪,只留原图
    doc.pages[0].figures.append(ParsedFigure(
        id="p1_fig_orig", page_number=1, bbox=(0.0, 0.0, float(width), float(height)),
        kind=KIND_EMBEDDED_BITMAP, image_bytes=src.data,
    ))


def pages_from_texts(page_texts: list[str]) -> ParsedDocument:
    """兜底:结果里没有 content_list 时,把按页文本构造成 ParsedDocument(整页一段,无图形)。"""
    pages: list[ParsedPage] = []
    for index, text in enumerate(page_texts):
        page_number = index + 1
        page = ParsedPage(
            page_number=page_number,
            width=PAGE_W,
            height=PAGE_H,
            is_scanned=False,
        )
        clean = text.strip()
        if clean:
            page.paragraphs.append(ParsedParagraph(
                id=f"p{page_number}_mineru",
                text=clean,
                page_number=page_number,
                bbox=(0.0, 0.0, PAGE_W, PAGE_H),
                section=None,
            ))
        pages.append(page)
    return ParsedDocument(page_count=len(pages), pages=pages)
