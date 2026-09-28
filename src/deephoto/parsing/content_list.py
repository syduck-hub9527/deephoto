"""MinerU content_list.json 的结构化解析,及由此构造 ParsedDocument。

字段格式依据 opendatalab/MinerU 源码(mineru/render/_internal/content_list/v1.py)的 V1 输出:

  text      {type:"text", text, text_level?, page_idx, bbox}      text_level 存在即标题
  image     {type:"image", img_path, image_caption[], image_footnote[], content?}
  table     {type:"table", img_path, table_caption[], table_footnote[], table_body?(HTML)}
  chart     {type:"chart", img_path, chart_caption[], chart_footnote[], content?}
  equation  {type:"equation", text?(latex), img_path}
  list      {type:"list", list_items[]}
  code      {type:"code", code_body, code_caption[]}
  其余(header/footer/page_number/aside_text 等)属页面噪声,丢弃。

bbox 为 0~1000 归一化坐标;页码 page_idx 从 0 起。
注意:以上是开源版源码的格式,MinerU 云端 ZIP 的实际字段以真实样本为准(见 DEEPHOTO_MINERU_DUMP_DIR)。
本模块对未知 type、缺失字段一律容错(忽略而非报错)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from .base import KIND_PARSER_IMAGE, ParsedCaption, ParsedDocument, ParsedFigure, ParsedPage, ParsedParagraph
from .captions import match_caption

# 页面尺寸占位(MinerU 不返回页面尺寸);bbox 由 0~1000 归一化坐标按此换算,仅为近似占位
PAGE_W = 612.0
PAGE_H = 792.0

# 只有层级不超过此值的标题才切换 section(更深层级的小标题当普通段落,避免切出过碎的块)
SECTION_MAX_LEVEL = 3
# 表格/图表文本按此长度拆成多个段落,每段重复图注与表头行
TABLE_PARA_CHARS = 800

_NOISE_TYPES = {"header", "footer", "page_number", "aside_text"}


@dataclass
class ContentElement:
    kind: str                       # heading|text|list|equation|code|footnote|image|table|chart
    page_idx: int                   # 0 起
    text: str = ""                  # 标题/正文/公式/代码/表格与图表的文本内容
    level: int = 0                  # 标题层级(text_level)
    caption: str = ""               # 图注/表注(多条以空格连接)
    footnote: str = ""
    image_path: str | None = None   # ZIP 内图片路径(img_path)
    image_bytes: bytes | None = None
    bbox: tuple[float, float, float, float] | None = None   # 0~1000 归一化


# ---------- content_list -> ContentElement ----------

def _str_list(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value if str(v).strip())
    return ""


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return tuple(float(v) for v in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def parse_content_list(items: list[dict], images: dict[str, bytes] | None = None) -> list[ContentElement]:
    """把 content_list 项转成带类型的元素(保持阅读顺序)。images: ZIP 内路径 -> 字节。"""
    images = images or {}
    elements: list[ContentElement] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            page_idx = int(item.get("page_idx"))
        except (TypeError, ValueError):
            continue
        kind_raw = str(item.get("type") or "")
        if kind_raw in _NOISE_TYPES:
            continue
        common = {"page_idx": page_idx, "bbox": _bbox(item.get("bbox"))}
        text = str(item.get("text") or "").strip()

        if kind_raw == "text":
            if not text:
                continue
            level = item.get("text_level")
            if isinstance(level, int) and level >= 1:
                elements.append(ContentElement(kind="heading", text=text, level=level, **common))
            else:
                elements.append(ContentElement(kind="text", text=text, **common))
        elif kind_raw == "page_footnote":
            if text:
                elements.append(ContentElement(kind="footnote", text=text, **common))
        elif kind_raw == "equation":
            if text:
                elements.append(ContentElement(kind="equation", text=text, **common))
        elif kind_raw == "list":
            lines = [str(x).strip() for x in item.get("list_items") or [] if str(x).strip()]
            if lines:
                elements.append(ContentElement(kind="list", text="\n".join(lines), **common))
        elif kind_raw == "code":
            body = str(item.get("code_body") or "").strip()
            if body:
                elements.append(ContentElement(kind="code", text=body, caption=_str_list(item.get("code_caption")),
                                               **common))
        elif kind_raw in ("image", "table", "chart"):
            path = item.get("img_path") if isinstance(item.get("img_path"), str) else None
            body = item.get("table_body") if kind_raw == "table" else item.get("content")
            elements.append(ContentElement(
                kind=kind_raw,
                text=html_to_text(str(body or "")),
                caption=_str_list(item.get(f"{kind_raw}_caption")),
                footnote=_str_list(item.get(f"{kind_raw}_footnote")),
                image_path=path or None,
                image_bytes=find_image(images, path),
                **common,
            ))
        # 未知 type:忽略
    return elements


def find_image(images: dict[str, bytes], path: str | None) -> bytes | None:
    """按 img_path 在 ZIP 图片表里找字节:先精确匹配,再按后缀路径,最后按文件名。"""
    if not path or not images:
        return None
    norm = path.replace("\\", "/").lstrip("./")
    if norm in images:
        return images[norm]
    for name, data in images.items():
        if name.endswith("/" + norm):
            return data
    base = norm.rsplit("/", 1)[-1]
    matches = [data for name, data in images.items() if name.rsplit("/", 1)[-1] == base]
    return matches[0] if len(matches) == 1 else None


# ---------- HTML 表格 -> 文本行 ----------

class _TableTextParser(HTMLParser):
    """容错的表格文本提取:缺少 </td>/</tr> 时,遇到下一个 <td>/<tr> 或结尾自动收口。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self.loose: list[str] = []

    def _end_cell(self) -> None:
        if self._cell is not None:
            if self._row is None:
                self._row = []
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None

    def _end_row(self) -> None:
        self._end_cell()
        if self._row is not None and any(self._row):
            self.rows.append(self._row)
        self._row = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._end_row()
            self._row = []
        elif tag in ("td", "th"):
            self._end_cell()
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._end_cell()
        elif tag == "tr":
            self._end_row()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif data.strip():
            self.loose.append(data.strip())

    def close(self):
        super().close()
        self._end_row()


def html_to_text(raw: str) -> str:
    """表格 HTML -> 每行一行、单元格以 ' | ' 分隔的文本;非 HTML 文本原样(去首尾空白)返回。

    合并单元格(colspan/rowspan)不展开,按出现顺序输出;复杂表格的版面以原图为准。
    """
    raw = (raw or "").strip()
    if not raw or "<" not in raw:
        return raw
    parser = _TableTextParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw).strip()
    if parser.rows:
        return "\n".join(" | ".join(cell for cell in row) for row in parser.rows)
    return " ".join(parser.loose)


# ---------- ContentElement -> ParsedDocument ----------

def _px(bbox: tuple[float, float, float, float] | None) -> tuple[float, float, float, float]:
    if bbox is None:
        return (0.0, 0.0, PAGE_W, PAGE_H)
    x0, y0, x1, y1 = bbox
    return (x0 * PAGE_W / 1000, y0 * PAGE_H / 1000, x1 * PAGE_W / 1000, y1 * PAGE_H / 1000)


def _table_paragraphs(caption: str, body: str, footnote: str) -> list[str]:
    """表格/图表文本 -> 若干段落文本;每段以图注开头,续段重复表头行,避免被硬切后丢上下文。"""
    rows = [line for line in body.splitlines() if line.strip()]
    if not rows:
        return [t for t in (caption, footnote) if t]
    header = rows[0]
    out: list[str] = []
    current: list[str] = []
    size = 0
    for index, row in enumerate(rows):
        if current and size + len(row) + 1 > TABLE_PARA_CHARS:
            out.append("\n".join(([caption] if caption else []) + current))
            current, size = [header] if index else [], len(header) if index else 0
        current.append(row)
        size += len(row) + 1
    if current:
        out.append("\n".join(([caption] if caption else []) + current))
    if footnote:
        out[-1] += "\n" + footnote
    return out


def build_document(elements: list[ContentElement], page_count: int) -> ParsedDocument:
    """按阅读顺序把元素装配成 ParsedDocument:段落带 section,图/表/图表带图注与原图字节。"""
    pages = [ParsedPage(page_number=i + 1, width=PAGE_W, height=PAGE_H, is_scanned=False)
             for i in range(page_count)]
    headings: dict[int, str] = {}      # level -> 标题文本
    counters = {"para": 0, "cap": 0, "fig": 0}

    def section() -> str | None:
        return " > ".join(headings[k] for k in sorted(headings)) or None

    def add_paragraph(page: ParsedPage, text: str, bbox) -> None:
        text = text.strip()
        if not text:
            return
        counters["para"] += 1
        page.paragraphs.append(ParsedParagraph(
            id=f"p{page.page_number}_c{counters['para']}", text=text,
            page_number=page.page_number, bbox=_px(bbox), section=section(),
        ))

    for el in elements:
        if not 0 <= el.page_idx < page_count:
            continue
        page = pages[el.page_idx]

        if el.kind == "heading":
            if el.level <= SECTION_MAX_LEVEL:
                for lv in [k for k in headings if k >= el.level]:
                    del headings[lv]
                headings[el.level] = el.text
            add_paragraph(page, el.text, el.bbox)
        elif el.kind in ("text", "list", "equation", "footnote"):
            add_paragraph(page, el.text, el.bbox)
        elif el.kind == "code":
            add_paragraph(page, (el.caption + "\n" if el.caption else "") + el.text, el.bbox)
        elif el.kind in ("image", "table", "chart"):
            caption_id = None
            if el.caption:
                counters["cap"] += 1
                caption_id = f"p{page.page_number}_cap{counters['cap']}"
                number, caption_text = match_caption(el.caption)
                page.captions.append(ParsedCaption(
                    id=caption_id, text=caption_text, figure_number=number,
                    page_number=page.page_number, bbox=_px(el.bbox),
                ))
            # 文本:图注必须落进段落(图文关联按“图注文本出现在块内”判定 caption_of)
            if el.kind == "image":
                extra = f"图内文字:{el.text}" if el.text else ""
                add_paragraph(page, "\n".join(t for t in (el.caption, extra, el.footnote) if t), el.bbox)
            else:
                for text in _table_paragraphs(el.caption, el.text, el.footnote):
                    add_paragraph(page, text, el.bbox)
            # 原图:字节缺失则不建 figure(不回退到本地渲染,避免用近似 bbox 裁出错图)
            if el.image_bytes:
                counters["fig"] += 1
                page.figures.append(ParsedFigure(
                    id=f"p{page.page_number}_fig{counters['fig']}", page_number=page.page_number,
                    bbox=_px(el.bbox), kind=KIND_PARSER_IMAGE, image_bytes=el.image_bytes,
                    caption_id=caption_id,
                ))
    return ParsedDocument(page_count=page_count, pages=pages)


__all__ = ["ContentElement", "PAGE_W", "PAGE_H", "build_document", "find_image", "html_to_text",
           "parse_content_list"]
