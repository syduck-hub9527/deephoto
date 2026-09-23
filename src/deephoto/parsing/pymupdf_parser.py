"""PyMuPDF 版面解析器(首版默认实现)。

策略(对应开发文档§3.2):
- 嵌入位图:直接提取原始图像字节;
- 矢量/混合图:cluster_drawings 聚类出图形区域,渲染页面后按 bbox 裁剪;
- 图注:行首匹配“图 N / Fig. N / Table N”的文本块,与同页最近图形区域配对;
- 有图号但配不到图形的图注:整页渲染回退,needs_review;
- 扫描页:正文文字极少且被整页图像覆盖,整页图作为输入,needs_review。

已知限制:多栏排版的阅读顺序为近似值(未做栏检测);复杂跨页图、浮动图
依赖 needs_review 人工抽检。替换 Docling 时实现 parsing.base.LayoutParser 即可。
"""

from __future__ import annotations

import statistics

import pymupdf as fitz  # PyMuPDF>=1.24;旧名 `import fitz` 已弃用

from .base import (
    KIND_EMBEDDED_BITMAP,
    KIND_PAGE_FALLBACK,
    KIND_VECTOR_RENDER,
    BBox,
    ParsedCaption,
    ParsedDocument,
    ParsedFigure,
    ParsedPage,
    ParsedParagraph,
)
from .captions import match_caption

_RENDER_ZOOM = 2.5          # 裁图渲染倍率(约 180 DPI)
_PAGE_PREVIEW_DPI = 150
_MIN_FIGURE_SIDE_PT = 40    # 过小的图形区域视为图标/装饰,忽略
_MAX_CAPTION_GAP_BELOW_PT = 200   # 图注在图形下方的最大间距
_MAX_CAPTION_GAP_ABOVE_PT = 60    # 图注在图形上方(表格常见)的最大间距


class PyMuPDFParser:
    def parse(self, pdf_bytes: bytes) -> ParsedDocument:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            median_size = _median_span_size(doc)
            pages: list[ParsedPage] = []
            for page_index in range(doc.page_count):
                pages.append(self._parse_page(doc, page_index, median_size))
            return ParsedDocument(page_count=doc.page_count, pages=pages)
        finally:
            doc.close()

    # ---- 渲染(供入库与 API 页预览使用)----

    def render_page(self, pdf_bytes: bytes, page_number: int, dpi: int = _PAGE_PREVIEW_DPI) -> bytes:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            page = doc.load_page(page_number - 1)
            zoom = dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()

    def render_region(self, pdf_bytes: bytes, page_number: int, bbox: BBox, zoom: float = _RENDER_ZOOM) -> bytes:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            page = doc.load_page(page_number - 1)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(*bbox), alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()

    # ---- 内部:逐页解析 ----

    def _parse_page(self, doc: fitz.Document, page_index: int, median_size: float) -> ParsedPage:
        page = doc.load_page(page_index)
        page_number = page_index + 1
        page_rect = page.rect

        text_dict = page.get_text("dict")
        blocks = text_dict.get("blocks", [])

        page_text_len = sum(
            len(span.get("text", ""))
            for b in blocks if b.get("type") == 0
            for line in b.get("lines", [])
            for span in line.get("spans", [])
        )

        paragraphs: list[ParsedParagraph] = []
        captions: list[ParsedCaption] = []
        figures: list[ParsedFigure] = []
        current_section: str | None = None

        for block_index, block in enumerate(blocks):
            bbox = tuple(float(v) for v in block["bbox"])
            if block.get("type") == 1:
                # 嵌入位图
                image_bytes = block.get("image")
                if image_bytes and _big_enough(bbox):
                    figures.append(ParsedFigure(
                        id=f"p{page_number}_img{block_index}",
                        page_number=page_number,
                        bbox=bbox,
                        kind=KIND_EMBEDDED_BITMAP,
                        image_bytes=image_bytes,
                    ))
                continue
            if block.get("type") != 0:
                continue
            text = _block_text(block)
            if not text:
                continue
            element_id = f"p{page_number}_b{block_index}"
            figure_number, caption_text = match_caption(text)
            if figure_number is not None:
                captions.append(ParsedCaption(
                    id=element_id, text=caption_text,
                    figure_number=figure_number, page_number=page_number, bbox=bbox,
                ))
                continue
            if _looks_like_heading(block, text, median_size):
                current_section = text
                continue
            paragraphs.append(ParsedParagraph(
                id=element_id, text=text, page_number=page_number,
                bbox=bbox, section=current_section,
            ))

        # 矢量/混合图:聚类绘图对象
        figures.extend(self._vector_figures(page, page_number, blocks))

        # 扫描页判定:文字极少且存在覆盖大部分页面的位图
        is_scanned = page_text_len < 20 and any(
            _area_ratio(f.bbox, page_rect) > 0.6 for f in figures
        )

        # 图注与图形区域配对(同页最近距离)
        _pair_captions(captions, figures)

        # 有图号但配不到区域的图注 -> 整页回退,待校验
        for caption in captions:
            if caption.figure_number and not any(f.caption_id == caption.id for f in figures):
                figures.append(ParsedFigure(
                    id=f"p{page_number}_fb_{caption.id}",
                    page_number=page_number,
                    bbox=(page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1),
                    kind=KIND_PAGE_FALLBACK,
                    image_bytes=None,   # 入库时按整页渲染
                    caption_id=caption.id,
                ))

        if is_scanned and not any(f.kind == KIND_PAGE_FALLBACK for f in figures):
            figures.append(ParsedFigure(
                id=f"p{page_number}_scan",
                page_number=page_number,
                bbox=(page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1),
                kind=KIND_PAGE_FALLBACK,
                image_bytes=None,
            ))

        return ParsedPage(
            page_number=page_number,
            width=page_rect.width,
            height=page_rect.height,
            is_scanned=is_scanned,
            paragraphs=paragraphs,
            captions=captions,
            figures=figures,
        )

    def _vector_figures(self, page: fitz.Page, page_number: int, blocks: list[dict]) -> list[ParsedFigure]:
        """矢量绘图聚类成图形区域;与位图重叠的丢弃(位图优先)。"""
        try:
            clusters = page.cluster_drawings()
        except AttributeError:
            return []   # PyMuPDF 版本过低,无聚类能力
        bitmap_boxes = [tuple(b["bbox"]) for b in blocks if b.get("type") == 1]
        text_boxes = [tuple(b["bbox"]) for b in blocks if b.get("type") == 0]
        figures: list[ParsedFigure] = []
        for i, rect in enumerate(clusters):
            bbox = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
            if not _big_enough(bbox):
                continue
            if any(_iou(bbox, tb) > 0.3 for tb in bitmap_boxes):
                continue
            # 图形区域若几乎全是文字块(如纯文本框),不当作图
            covered = sum(_iou(bbox, tb) for tb in text_boxes)
            if covered > 0.8:
                continue
            figures.append(ParsedFigure(
                id=f"p{page_number}_vec{i}",
                page_number=page_number,
                bbox=bbox,
                kind=KIND_VECTOR_RENDER,
                image_bytes=None,   # 入库时按区域渲染
            ))
        return figures


# ---- 纯函数辅助 ----

def _block_text(block: dict) -> str:
    parts: list[str] = []
    for line in block.get("lines", []):
        line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
        if line_text:
            parts.append(line_text)
    return "\n".join(parts).strip()


def _median_span_size(doc: fitz.Document) -> float:
    sizes: list[float] = []
    for page_index in range(min(doc.page_count, 30)):   # 采样前 30 页足够
        text_dict = doc.load_page(page_index).get_text("dict")
        for b in text_dict.get("blocks", []):
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        sizes.append(float(span.get("size", 0)))
    return statistics.median(sizes) if sizes else 10.0


def _looks_like_heading(block: dict, text: str, median_size: float) -> bool:
    if len(text) > 120 or "\n" in text:
        return False
    spans = [s for line in block.get("lines", []) for s in line.get("spans", []) if s.get("text", "").strip()]
    if not spans:
        return False
    max_size = max(float(s.get("size", 0)) for s in spans)
    all_bold = all((int(s.get("flags", 0)) & 16) != 0 for s in spans)
    return max_size >= median_size * 1.2 or (all_bold and len(text) <= 60)


def _big_enough(bbox: BBox) -> bool:
    return (bbox[2] - bbox[0]) >= _MIN_FIGURE_SIDE_PT and (bbox[3] - bbox[1]) >= _MIN_FIGURE_SIDE_PT


def _iou(a: BBox, b: BBox) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _area_ratio(bbox: BBox, page_rect: fitz.Rect) -> float:
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    total = page_rect.width * page_rect.height
    return area / total if total > 0 else 0.0


def _pair_captions(captions: list[ParsedCaption], figures: list[ParsedFigure]) -> None:
    """图注与同页最近图形区域配对;优先取图注在图形下方的布局。"""
    pairable = [f for f in figures if f.kind != KIND_PAGE_FALLBACK]
    for caption in captions:
        best: tuple[float, ParsedFigure] | None = None
        for figure in pairable:
            if figure.page_number != caption.page_number or figure.caption_id:
                continue
            below_gap = caption.bbox[1] - figure.bbox[3]           # 图注顶部 - 图形底部
            above_gap = figure.bbox[1] - caption.bbox[3]           # 图形顶部 - 图注底部
            if 0 <= below_gap <= _MAX_CAPTION_GAP_BELOW_PT:
                gap = below_gap
            elif 0 <= above_gap <= _MAX_CAPTION_GAP_ABOVE_PT:
                gap = above_gap
            else:
                continue
            # 水平方向需有重叠,避免配到远处侧栏的图
            if min(caption.bbox[2], figure.bbox[2]) <= max(caption.bbox[0], figure.bbox[0]):
                continue
            if best is None or gap < best[0]:
                best = (gap, figure)
        if best is not None:
            best[1].caption_id = caption.id
