"""Word(.docx)本地解析(python-docx;DEV_multi_format_documents.md §3.4b)。

- 段落与表格按文档顺序交错遍历(element.body 子节点);
- 标题三级兜底:样式名 Heading N → w:outlineLvl → 样式名 标题 N(WPS);
- 图片取段内 a:blip 的嵌入字节(含 content_type);EMF/WMF/SVG 跳过并告警
  (Pillow 对这些格式支持依平台而异),其余格式由入库处统一转 PNG;
- 图注:紧邻图前/后的 Caption 样式段或 match_caption 命中段;
- 表格复用 content_list._table_paragraphs(续段重复表头);合并单元格不展开;
- 页眉页脚/批注/修订忽略(修订文本视角为 python-docx 默认,未单独验证);
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
        pending_caption: str | None = None   # 紧邻图片之前的图注样式段

        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                para = Paragraph(child, document)
                text = para.text.strip()
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
                    pending_caption = None   # 空段打断图注邻接
                    continue
                if caption_like and blocks and blocks[-1].kind == "image" \
                        and blocks[-1].caption is None:
                    blocks[-1].caption = text   # 图注在图片之后
                level = _heading_level(para, style, qn)
                if level:
                    blocks.append(LocalBlock("heading", text=text, level=level))
                    pending_caption = None
                else:
                    if caption_like:
                        pending_caption = text
                    blocks.append(LocalBlock("text", text=text))
            elif child.tag == qn("w:tbl"):
                pending_caption = None
                table = Table(child, document)
                rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
                rows = [r for r in rows if r.strip(" |")]
                for piece in _table_paragraphs("", "\n".join(rows), ""):
                    blocks.append(LocalBlock("text", text=piece))

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


def _heading_level(para, style_name: str, qn) -> int | None:
    """标题层级三级兜底:样式名 Heading N → w:outlineLvl(0 起)→ 样式名 标题 N(WPS)。"""
    m = _HEADING_EN_RE.match(style_name)
    if m:
        return int(m.group(1))
    ppr = para._p.pPr
    if ppr is not None:
        outline = ppr.find(qn("w:outlineLvl"))
        if outline is not None:
            try:
                return int(outline.get(qn("w:val"))) + 1
            except (TypeError, ValueError):
                pass
    m = _HEADING_CN_RE.match(style_name)
    if m:
        return int(m.group(1))
    return None
