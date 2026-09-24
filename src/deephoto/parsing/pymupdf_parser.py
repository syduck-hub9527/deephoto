"""基于 poppler 的版面解析器(替代原 PyMuPDF 实现)。

策略(对应开发文档§3.2 的简化版,无 PyMuPDF 依赖):
- 文字版:pdftotext 逐页提取文字,按行聚成段落;
- 图注:行首匹配“图 N / Fig. N / Table N”,尽量配给同页整页回退图;
- 扫描页:正文文字极少的页,整页渲染作为图,needs_review;
- 不再做嵌入位图提取与矢量聚类(那是 PyMuPDF 的能力),统一整页回退。

渲染仍走 poppler(pdf_backend),供入库与页预览使用。
"""

from __future__ import annotations

from . import pdf_backend
from .base import (
    KIND_PAGE_FALLBACK,
    BBox,
    ParsedCaption,
    ParsedDocument,
    ParsedFigure,
    ParsedPage,
    ParsedParagraph,
)
from .captions import match_caption

# A4 约 612×792 点;无真实版面时给一个稳定占位尺寸
_PAGE_W = 612.0
_PAGE_H = 792.0
# 文字少于该值的页视为扫描页
_SCAN_TEXT_THRESHOLD = 20


class PopplerParser:
    def parse(self, pdf_bytes: bytes) -> ParsedDocument:
        texts = pdf_backend.page_texts(pdf_bytes)
        pages = [self._parse_page(i + 1, text) for i, text in enumerate(texts)]
        return ParsedDocument(page_count=len(pages), pages=pages)

    # ---- 渲染(供入库与 API 页预览使用)----

    def render_page(self, pdf_bytes: bytes, page_number: int, dpi: int = 150) -> bytes:
        return pdf_backend.render_page(pdf_bytes, page_number, dpi)

    def render_region(self, pdf_bytes: bytes, page_number: int, bbox: BBox, zoom: float = 2.5) -> bytes:
        return pdf_backend.render_region(pdf_bytes, page_number, bbox)

    # ---- 内部 ----

    def _parse_page(self, page_number: int, text: str) -> ParsedPage:
        is_scanned = len(text.strip()) < _SCAN_TEXT_THRESHOLD
        paragraphs: list[ParsedParagraph] = []
        captions: list[ParsedCaption] = []
        figures: list[ParsedFigure] = []
        page_bbox: BBox = (0.0, 0.0, _PAGE_W, _PAGE_H)

        for line_index, raw in enumerate(text.splitlines()):
            line = raw.strip()
            if not line:
                continue
            element_id = f"p{page_number}_b{line_index}"
            figure_number, caption_text = match_caption(line)
            if figure_number is not None:
                captions.append(ParsedCaption(
                    id=element_id, text=caption_text,
                    figure_number=figure_number, page_number=page_number, bbox=page_bbox,
                ))
                continue
            paragraphs.append(ParsedParagraph(
                id=element_id, text=line, page_number=page_number,
                bbox=page_bbox, section=None,
            ))

        # 扫描页:整页渲染作为回退图,待校验
        if is_scanned:
            figures.append(ParsedFigure(
                id=f"p{page_number}_scan",
                page_number=page_number,
                bbox=page_bbox,
                kind=KIND_PAGE_FALLBACK,
                image_bytes=None,   # 入库时按整页渲染
            ))

        # 把识别出的图注尽量配给整页回退图
        for caption, figure in zip(captions, figures):
            if figure.caption_id is None:
                figure.caption_id = caption.id

        return ParsedPage(
            page_number=page_number,
            width=_PAGE_W,
            height=_PAGE_H,
            is_scanned=is_scanned,
            paragraphs=paragraphs,
            captions=captions,
            figures=figures,
        )


# 兼容旧引用(app/ingest 通过别名拿到解析器),不再依赖 PyMuPDF。
PyMuPDFParser = PopplerParser
