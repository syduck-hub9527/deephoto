"""Word(.docx)本地解析(python-docx;DEV_multi_format_documents.md §3.4b)。

- 段落与表格按文档顺序交错遍历(element.body 子节点);
- 标题四级兜底:样式名 Heading N → 段落自身 w:outlineLvl → 样式名 标题 N(WPS)
  → 样式定义里的 w:outlineLvl(自定义样式,沿 w:basedOn 向上查);
- 图片取段内 a:blip 的嵌入字节(含 content_type);EMF/WMF/SVG 跳过并告警
  (Pillow 对这些格式支持依平台而异),其余格式由入库处统一转 PNG;
- 图注:紧邻图前/后的 Caption 样式段或 match_caption 命中段;
- 表格复用 content_list._table_paragraphs(续段重复表头);合并单元格不展开,
  横向合并按底层单元格对象去重(不重复输出);
- 页眉页脚/批注忽略;修订按接受后的视角:w:ins(新增)收入、w:del(删除)跳过
  (python-docx 默认视图不含 w:ins,会丢改动内容,故自实现文本提取);
- 块级容器递归展开:w:sdt(内容控件,常见于目录/封面/模板)进 w:sdtContent,
  w:customXml / w:smartTag 直接展开;行级 w:sdt(段内包裹 run)不在此列;
- 正文几乎为空但含绘图对象(内容可能在文本框/形状里):回退 MinerU 云端
  (需 Token,否则报可读错误)。
"""

from __future__ import annotations

import io
import logging
import re

from ..config import Settings
from .captions import match_caption
from .content_list import _table_paragraphs
from .formats import check_zip_safety
from .paginate import LocalBlock, build_local_document
from .registry import EngineUnavailable, SourceFile

logger = logging.getLogger(__name__)

_HEADING_EN_RE = re.compile(r"^Heading\s+(\d+)$", re.IGNORECASE)
_HEADING_CN_RE = re.compile(r"^标题\s*(\d+)$")
# Pillow 对 EMF/WMF/SVG 的支持依平台而异(未验证):跳过并告警,不影响入库
_NON_RASTER_MIMES = {"image/x-emf", "image/x-wmf", "image/svg+xml", "image/emf", "image/wmf"}
# 本地解析正文的最低字数;低于此且含绘图对象时判定内容在文本框/形状里,回退 MinerU
_MIN_TEXT_CHARS = 50


class DocxParser:
    """docx -> ParsedDocument(locator_kind='section',经虚拟分页)。纯本地,无网络。"""

    engine = "local"

    def __init__(self, settings: Settings):
        self._settings = settings

    def parse(self, src: SourceFile, observer):
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        check_zip_safety(src.data, self._settings.max_zip_uncompressed_mb)
        document = Document(io.BytesIO(src.data))

        blocks: list[LocalBlock] = []
        pending_caption: str | None = None   # 紧邻图片之前的图注样式段(暂存,未出文本块)
        # 样式表按 style_id 索引:_heading_level 的第四级兜底(样式定义里的大纲级别)用
        styles_by_id = {getattr(s, "style_id", None): s for s in document.styles}
        styles_by_id.pop(None, None)

        def flush_pending() -> None:
            nonlocal pending_caption
            if pending_caption:
                blocks.append(LocalBlock("text", text=pending_caption))
                pending_caption = None

        for child in _iter_block_items(document.element.body, qn):
            if child.tag == qn("w:p"):
                para = Paragraph(child, document)
                text = _paragraph_text(para, qn).strip()
                style = para.style.name if para.style is not None else ""
                images = self._images_in(para, document, observer, qn)
                caption_like = bool(text) and (
                    style.lower() == "caption" or match_caption(text)[0] is not None)

                if images:
                    caption = pending_caption
                    pending_caption = None
                    if text:
                        blocks.append(LocalBlock("text", text=text))
                    for image_bytes in images:
                        blocks.append(LocalBlock("image", image_bytes=image_bytes,
                                                 caption=caption))
                        caption = None       # 同段多图:图注只配第一张
                    continue
                if not text:
                    flush_pending()          # 空段打断图注邻接;暂存图注落为普通文本
                    continue
                if caption_like and blocks and blocks[-1].kind == "image" \
                        and blocks[-1].caption is None:
                    # 图注在图片之后:配对即完成——不再设为 pending(否则被下一张图抢走),
                    # 也不再单独出文本块(装配层会为图片把图注落进段落,文本只此一份)
                    blocks[-1].caption = text
                    continue
                level = _heading_level(para, style, qn, styles_by_id)
                if level:
                    flush_pending()
                    blocks.append(LocalBlock("heading", text=text, level=level))
                elif caption_like:
                    # 候选前置图注:暂存不出文本块;被紧随的图取走则只归图,
                    # 否则在下个非图内容/空段/结尾落回普通文本(不丢)
                    flush_pending()
                    pending_caption = text
                else:
                    flush_pending()
                    blocks.append(LocalBlock("text", text=text))
            elif child.tag == qn("w:tbl"):
                flush_pending()              # 表格不配图注;暂存段先落为普通文本
                lines: list[str] = []
                images: list[bytes] = []
                self._walk_table(Table(child, document), lines, images, observer, qn, document)
                for piece in _table_paragraphs("", "\n".join(lines), ""):
                    blocks.append(LocalBlock("text", text=piece))
                # 单元格里的图片:表后按出现顺序出图块(图注配对不进入表格内)
                for image_bytes in images:
                    blocks.append(LocalBlock("image", image_bytes=image_bytes))
        flush_pending()                      # 结尾未被取走的暂存图注落回普通文本

        # 回退:正文几乎为空但含绘图对象 -> 内容可能在文本框/形状里,本地读不到
        text_chars = sum(len(b.text) for b in blocks if b.kind in ("text", "heading"))
        has_drawing = bool(
            document.element.body.findall(".//" + qn("w:drawing"))
            or document.element.body.findall(".//" + qn("w:pict")))
        if text_chars < _MIN_TEXT_CHARS and has_drawing:
            if not self._settings.mineru_api_key:
                raise EngineUnavailable(
                    "该 docx 正文几乎为空且含绘图对象(内容可能在文本框/形状中,本地解析读不到);"
                    "需要 MinerU 云端解析:请配置 DEEPHOTO_MINERU_API_KEY,"
                    "或设 DEEPHOTO_DOCX_PARSER=mineru")
            logger.warning("docx 本地解析内容过少(%d 字,含绘图对象),回退 MinerU", text_chars)
            observer.warn("本地解析读到的正文过少(内容可能在文本框/形状里),已改用云端解析")
            from .mineru_parser import MinerUParser
            return MinerUParser(self._settings).parse(src, observer)

        return build_local_document(blocks)

    def _images_in(self, para, document, observer, qn) -> list[bytes]:
        """段内 a:blip 的 r:embed → part.blob(inline 与 anchor 浮动图都覆盖)。"""
        blip = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
        embed = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
        out: list[bytes] = []
        for node in para._p.iter(blip):
            rid = node.get(embed)
            if not rid or rid not in document.part.related_parts:
                continue
            part = document.part.related_parts[rid]
            content_type = getattr(part, "content_type", "") or ""
            if content_type.lower() in _NON_RASTER_MIMES:
                observer.warn(f"跳过暂不支持的图片格式({content_type}),其余内容不受影响")
                continue
            out.append(part.blob)
        return out

    def _walk_table(self, table, lines: list[str], images: list[bytes],
                    observer, qn, document) -> None:
        """表格按行展平为文本行;单元格里的嵌套表格递归展平、图片收集成图块。

        横向合并区域 row.cells 会重复返回同一单元格,按底层 tc 对象去重
        (合并单元格不展开也不重复)。
        """
        for row in table.rows:
            texts: list[str] = []
            seen: set[int] = set()
            for cell in row.cells:
                key = id(cell._tc)
                if key in seen:
                    continue
                seen.add(key)
                before = len(lines)                # _walk_cell 至多追加一条拼接行
                self._walk_cell(cell, lines, images, observer, qn, document)
                cell_text = lines[before] if len(lines) > before else ""
                del lines[before:]
                if cell_text:
                    texts.append(cell_text)
            row_text = " | ".join(texts)
            if row_text.strip(" |"):
                lines.append(row_text)

    def _walk_cell(self, cell, lines: list[str], images: list[bytes],
                   observer, qn, document) -> None:
        """单元格内容按序展平:段落文本、嵌套表格(递归)、图片字节收集。"""
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        cell_lines: list[str] = []
        for child in _iter_block_items(cell._tc, qn):
            if child.tag == qn("w:p"):
                para = Paragraph(child, document)
                text = _paragraph_text(para, qn).strip()
                if text:
                    cell_lines.append(text)
                images.extend(self._images_in(para, document, observer, qn))
            else:                                # w:tbl:嵌套表格递归展平
                nested: list[str] = []
                self._walk_table(Table(child, document), nested, images,
                                 observer, qn, document)
                cell_lines.extend(nested)
        joined = "\n".join(cell_lines).strip()
        if joined:
            lines.append(joined)


def _iter_block_items(parent_el, qn):
    """产出 w:p / w:tbl 块级节点;块级容器递归展开:
    w:sdt 进 w:sdtContent(目录/封面/模板常用内容控件包正文),
    w:customXml / w:smartTag 直接展开其子节点。"""
    for child in parent_el.iterchildren():
        if child.tag in (qn("w:p"), qn("w:tbl")):
            yield child
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _iter_block_items(content, qn)
        elif child.tag in (qn("w:customXml"), qn("w:smartTag")):
            yield from _iter_block_items(child, qn)


def _paragraph_text(para, qn) -> str:
    """段落文本,接受修订视角:w:ins(新增)里的 run 收入,w:del(删除)子树跳过。

    python-docx 的 Paragraph.text 只覆盖 w:r / w:hyperlink 直接子节点,不含 w:ins
    里的 run(开着修订的合同/评审稿会丢改动内容)。本实现递归遍历,顺带覆盖
    行级 w:sdt/w:smartTag 与文本框(w:txbxContent)里的文字;域指令(w:instrText)跳过。
    """
    parts: list[str] = []
    _SKIP = {qn("w:del"), qn("w:delText"), qn("w:instrText")}

    def walk(el) -> None:
        for node in el.iterchildren():
            tag = node.tag
            if tag in _SKIP:
                continue
            if tag == qn("w:t"):
                parts.append(node.text or "")
            elif tag == qn("w:tab"):
                parts.append("\t")
            elif tag in (qn("w:br"), qn("w:cr")):
                parts.append("\n")
            else:
                walk(node)

    walk(para._p)
    return "".join(parts)


def _outline_level(ppr, qn) -> int | None:
    """w:pPr 里的 w:outlineLvl(0 起)→ 标题层级;没有返回 None。"""
    if ppr is None:
        return None
    outline = ppr.find(qn("w:outlineLvl"))
    if outline is None:
        return None
    try:
        return int(outline.get(qn("w:val"))) + 1
    except (TypeError, ValueError):
        return None


def _style_outline_level(style, styles_by_id: dict, qn, _depth: int = 0) -> int | None:
    """样式定义里的大纲级别;沿 w:basedOn 向上继承(深度上限防环)。"""
    if style is None or _depth > 10:
        return None
    element = getattr(style, "element", None)
    if element is None:
        return None
    level = _outline_level(element.find(qn("w:pPr")), qn)
    if level is not None:
        return level
    based_on = element.find(qn("w:basedOn"))
    parent_id = based_on.get(qn("w:val")) if based_on is not None else None
    return _style_outline_level(styles_by_id.get(parent_id), styles_by_id, qn, _depth + 1)


def _heading_level(para, style_name: str, qn, styles_by_id: dict | None = None) -> int | None:
    """标题层级四级兜底:样式名 Heading N → 段落自身 w:outlineLvl → 样式名 标题 N(WPS)
    → 样式定义里的 w:outlineLvl(自定义样式,沿 basedOn 向上查)。"""
    m = _HEADING_EN_RE.match(style_name)
    if m:
        return int(m.group(1))
    level = _outline_level(para._p.pPr, qn)
    if level is not None:
        return level
    m = _HEADING_CN_RE.match(style_name)
    if m:
        return int(m.group(1))
    if styles_by_id:
        return _style_outline_level(para.style, styles_by_id, qn)
    return None
