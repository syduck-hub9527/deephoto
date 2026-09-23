"""版面解析层的数据结构与协议。

解析器输出与具体引擎(PyMuPDF/Docling)解耦:入库流水线只依赖这里的类型。
bbox 一律为 PDF 点坐标(x0, y0, x1, y1),与数据库 bbox_coord='pdf_points' 对应。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

BBox = tuple[float, float, float, float]

# 图形区域来源
KIND_EMBEDDED_BITMAP = "embedded_bitmap"   # 嵌入位图,可直接提取原图
KIND_VECTOR_RENDER = "vector_render"       # 矢量/混合图,需渲染页面后裁剪
KIND_PAGE_FALLBACK = "page_fallback"       # 边界不确定,整页回退(needs_review)


@dataclass
class ParsedParagraph:
    id: str                    # 解析器元素 ID(页内稳定,如 "p3_b7")
    text: str
    page_number: int           # 1 起
    bbox: BBox
    section: str | None        # 所属章节标题(解析器尽力推断)


@dataclass
class ParsedCaption:
    id: str
    text: str                  # 完整图注文本
    figure_number: str | None  # 规范化图号,如 "3"、"2.1"(无前缀)
    page_number: int
    bbox: BBox


@dataclass
class ParsedFigure:
    id: str
    page_number: int
    bbox: BBox
    kind: str                  # KIND_* 常量之一
    image_bytes: bytes | None  # 位图原图(embedded_bitmap 时有值)
    caption_id: str | None = None  # 解析器配对的图注


@dataclass
class ParsedPage:
    page_number: int
    width: float
    height: float
    is_scanned: bool           # 正文文字极少且含整页图像 -> 扫描页
    paragraphs: list[ParsedParagraph] = field(default_factory=list)
    captions: list[ParsedCaption] = field(default_factory=list)
    figures: list[ParsedFigure] = field(default_factory=list)


@dataclass
class ParsedDocument:
    page_count: int
    pages: list[ParsedPage]

    def all_paragraphs(self) -> list[ParsedParagraph]:
        return [p for page in self.pages for p in page.paragraphs]

    def all_captions(self) -> list[ParsedCaption]:
        return [c for page in self.pages for c in page.captions]

    def all_figures(self) -> list[ParsedFigure]:
        return [f for page in self.pages for f in page.figures]


class LayoutParser(Protocol):
    """版面解析器协议:换 Docling 时实现同一接口即可。"""

    def parse(self, pdf_bytes: bytes) -> ParsedDocument: ...

    def render_page(self, pdf_bytes: bytes, page_number: int, dpi: int = 150) -> bytes:
        """渲染整页 PNG(页预览、整页回退图)。"""
        ...

    def render_region(self, pdf_bytes: bytes, page_number: int, bbox: BBox, zoom: float = 2.5) -> bytes:
        """按 PDF 点坐标裁剪渲染 PNG(矢量/混合图取图)。"""
        ...
