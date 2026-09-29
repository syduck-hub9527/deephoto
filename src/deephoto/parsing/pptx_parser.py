"""PPT(.pptx)本地解析(python-pptx;DEV_multi_format_documents.md §3.4c)。

- 一张幻灯片 = 一个 ParsedPage(locator_kind='slide',页码即幻灯片序号,天然有位置);
- 标题占位符 -> 该页 section("幻灯片 N:标题");标题文字本身仍按阅读顺序落为段落
  (标题可检索,与 docx/md 的标题处理一致);
- 形状按 (top, left) 排序当阅读顺序(python-pptx 返回的是层叠顺序,不是阅读顺序);
  位置缺失(None)按 0 处理,同位置保持层叠序(sorted 稳定);
- 文本框/占位符/自选形状的文字经 text_frame 提取;组合形状递归展开;
- 图片经 a:blip 的 r:embed 直取图片 part 字节(含 content_type,不经格式识别);
  EMF/WMF/SVG 跳过并告警(与 docx 同约定),其余格式由入库处统一转 PNG;
- 图注:阅读顺序上图片之后最近一条未配对文本,match_caption 命中且位置在图片下方、
  水平有重叠则配对(配对后不再单独出文本段落;位置信息缺失时退化为纯邻接判断);
- 表格按行展平(" | " 连接),复用 content_list._table_paragraphs(续段重复表头);
  被合并的格子(is_spanned)跳过,不重复输出;
- 讲者备注:该页最后一个段落,前缀"备注:";
- SmartArt/图表/OLE 嵌入对象 python-pptx 读不到(无文字无字节可取):跳过;
  整篇无任何文字与图时由 validate_parsed 报可读错误。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from ..config import Settings
from .base import (
    KIND_EMBEDDED_BITMAP,
    ParsedCaption,
    ParsedDocument,
    ParsedFigure,
    ParsedPage,
    ParsedParagraph,
)
from .captions import match_caption
from .content_list import PAGE_H, PAGE_W, _table_paragraphs
from .docx_parser import _NON_RASTER_MIMES
from .formats import check_zip_safety
from .registry import SourceFile

logger = logging.getLogger(__name__)


@dataclass
class _Item:
    """一页幻灯片上按阅读顺序展平的内容块。"""
    kind: str                      # text | image | table
    shape: object                  # 来源形状(图注位置判断用)
    text: str = ""                 # text/table: 文本内容
    image_bytes: bytes | None = None


def _reading_key(shape):
    """阅读顺序:先上后下、同排先左后右;位置缺失按 0(同位置保持原层叠序)。"""
    top = shape.top if shape.top is not None else 0
    left = shape.left if shape.left is not None else 0
    return (top, left)


def _below_overlaps(img, cap) -> bool:
    """图注位置约束:文本框在图片下方(顶边不低于图片顶边)且水平区间有重叠;
    任一位置信息缺失时放行(退化为纯邻接判断)。"""
    values = (img.top, img.left, img.width, cap.top, cap.left, cap.width)
    if any(v is None for v in values):
        return True
    if cap.top < img.top:
        return False
    return cap.left < img.left + img.width and img.left < cap.left + cap.width


def _table_text(table) -> str:
    """表格按行展平为文本行;被合并的格子(is_spanned,独立 tc 但无文字)跳过。"""
    lines: list[str] = []
    for row in table.rows:
        texts = [cell.text.strip() for cell in row.cells if not cell.is_spanned]
        row_text = " | ".join(t for t in texts if t)
        if row_text.strip(" |"):
            lines.append(row_text)
    return "\n".join(lines)


class PptxParser:
    """pptx -> ParsedDocument(locator_kind='slide',一张幻灯片一页)。纯本地,无网络。"""

    engine = "local"

    def __init__(self, settings: Settings):
        self._settings = settings

    def parse(self, src: SourceFile, observer):
        from pptx import Presentation

        check_zip_safety(src.data, self._settings.max_zip_uncompressed_mb)
        prs = Presentation(io.BytesIO(src.data))

        pages: list[ParsedPage] = []
        counters = {"para": 0, "cap": 0, "fig": 0}
        placeholder = (0.0, 0.0, PAGE_W, PAGE_H)

        def add_paragraph(page: ParsedPage, section: str, text: str) -> None:
            text = text.strip()
            if not text:
                return
            counters["para"] += 1
            page.paragraphs.append(ParsedParagraph(
                id=f"p{page.page_number}_c{counters['para']}", text=text,
                page_number=page.page_number, bbox=placeholder, section=section,
            ))

        for number, slide in enumerate(prs.slides, 1):
            page = ParsedPage(page_number=number, width=PAGE_W, height=PAGE_H, is_scanned=False)
            title_shape = slide.shapes.title
            title = (title_shape.text or "").strip() if title_shape is not None else ""
            section = f"幻灯片 {number}:{title}" if title else f"幻灯片 {number}"

            items = self._walk_shapes(slide.shapes, observer)
            consumed: set[int] = set()      # 已配对为图注的文本 item 下标
            for i, item in enumerate(items):
                if i in consumed:
                    continue
                if item.kind == "text":
                    add_paragraph(page, section, item.text)
                elif item.kind == "table":
                    for piece in _table_paragraphs("", item.text, ""):
                        add_paragraph(page, section, piece)
                else:                        # image
                    caption: str | None = None
                    j = _next_text(items, i, consumed)
                    if j is not None and match_caption(items[j].text)[0] is not None \
                            and _below_overlaps(item.shape, items[j].shape):
                        caption = items[j].text
                        consumed.add(j)
                    caption_id = None
                    if caption:
                        counters["cap"] += 1
                        caption_id = f"p{number}_cap{counters['cap']}"
                        num, caption_text = match_caption(caption)
                        page.captions.append(ParsedCaption(
                            id=caption_id, text=caption_text, figure_number=num,
                            page_number=number, bbox=placeholder,
                        ))
                    # 图注文本必须落进段落(图文关联按"图注文本出现在块内"判定 caption_of)
                    add_paragraph(page, section, caption or "")
                    counters["fig"] += 1
                    page.figures.append(ParsedFigure(
                        id=f"p{number}_fig{counters['fig']}", page_number=number,
                        bbox=placeholder, kind=KIND_EMBEDDED_BITMAP,
                        image_bytes=item.image_bytes, caption_id=caption_id,
                    ))

            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    add_paragraph(page, section, f"备注:{notes}")
            pages.append(page)

        return ParsedDocument(page_count=len(pages), pages=pages, locator_kind="slide")

    def _walk_shapes(self, shapes, observer) -> list[_Item]:
        """形状按阅读顺序展平:文本、图片、表格;组合形状递归(组内同样按阅读顺序)。"""
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        out: list[_Item] = []
        for shape in sorted(shapes, key=_reading_key):
            try:
                shape_type = shape.shape_type
            except NotImplementedError:
                shape_type = None           # 无预设几何等未识别形状:按文本/表格兜底
            if shape_type == MSO_SHAPE_TYPE.GROUP:
                out.extend(self._walk_shapes(shape.shapes, observer))
            elif shape_type == MSO_SHAPE_TYPE.PICTURE:
                blob = self._image_bytes(shape, observer)
                if blob:
                    out.append(_Item("image", shape, image_bytes=blob))
            elif getattr(shape, "has_table", False) and shape.has_table:
                body = _table_text(shape.table)
                if body:
                    out.append(_Item("table", shape, text=body))
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    out.append(_Item("text", shape, text=text))
        return out

    def _image_bytes(self, shape, observer) -> bytes | None:
        """图片字节:经 a:blip 的 r:embed 直取图片 part(与 docx 同法,不经 python-pptx 的
        格式识别,未知格式不会炸);链接图(r:link,无嵌入字节)跳过;
        EMF/WMF/SVG 跳过并告警(Pillow 支持依平台而异,与 docx 同约定)。"""
        from pptx.oxml.ns import qn

        blip = shape._pic.find(".//" + qn("a:blip"))
        rid = blip.get(qn("r:embed")) if blip is not None else None
        if not rid:
            return None
        part = shape.part.related_part(rid)
        content_type = (getattr(part, "content_type", "") or "").lower()
        if content_type in _NON_RASTER_MIMES:
            observer.warn(f"跳过暂不支持的图片格式({content_type}),其余内容不受影响")
            return None
        return part.blob


def _next_text(items: list[_Item], after: int, consumed: set[int]) -> int | None:
    """图片之后第一条未配对的文本 item 下标(跳过图/表与已配对的图注);
    找到的这条若配不上(非图注或位置不符)也不再往后找,避免抢走远处图注。"""
    for j in range(after + 1, len(items)):
        if items[j].kind == "text" and j not in consumed:
            return j
    return None
