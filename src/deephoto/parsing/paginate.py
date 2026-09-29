"""虚拟分页:无真实页码的格式(md/txt/docx)把阅读顺序的块映射到分段号。

分段号只用于顺序与邻近判断(nearby 关联、块边界),对用户展示走 section。
规则(DEV_multi_format_documents.md §3.2):
- 遇到 ≤2 级标题且当前段已 >= 800 字 -> 另起一段;
- 当前段累计超过 3000 字 -> 另起一段;
- 图片归属其出现位置所在段。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .base import (
    KIND_EMBEDDED_BITMAP,
    ParsedCaption,
    ParsedDocument,
    ParsedFigure,
    ParsedPage,
    ParsedParagraph,
)
from .captions import match_caption
from .content_list import PAGE_H, PAGE_W, SECTION_MAX_LEVEL

SEGMENT_HEADING_MIN_CHARS = 800    # 小标题另起一段的前提:当前段已有这么多字
SEGMENT_MAX_CHARS = 3000           # 单段字数上限(超过即切)
_HEADING_LEVEL = 2                 # 只有 <=2 级标题触发"标题分段"
# 超过此长度的文本块先在行边界拆开(单行超长则硬切),再参与分页;
# 否则一份单换行 txt / 超大围栏会整份落在一个分段里,nearby 失去局部性
_BLOCK_SPLIT_CHARS = 1500


@dataclass
class LocalBlock:
    """本地解析器的中间块(阅读顺序)。paginate 与 ParsedDocument 装配共用。"""
    kind: str                      # heading|text|image
    text: str = ""                 # 段落/标题文本(图片块为 alt)
    level: int = 0                 # 标题层级(1~6);0 表示非标题
    image_bytes: bytes | None = None
    caption: str | None = None     # 解析器配对到的图注原文


def paginate(blocks: list[LocalBlock]) -> list[int]:
    """返回每个块所属的分段号(1 起,与 blocks 等长)。空输入返回 []。"""
    numbers: list[int] = []
    segment = 1
    size = 0
    for block in blocks:
        if block.kind == "heading" and block.level <= _HEADING_LEVEL and size >= SEGMENT_HEADING_MIN_CHARS:
            segment += 1
            size = 0
        elif size > SEGMENT_MAX_CHARS:
            segment += 1
            size = 0
        numbers.append(segment)
        size += len(block.text)
    return numbers


def _split_oversize(blocks: list[LocalBlock]) -> list[LocalBlock]:
    """超过 _BLOCK_SPLIT_CHARS 的文本块按行边界拆开(单行超长硬切);标题/图片不动。"""
    out: list[LocalBlock] = []
    for block in blocks:
        if block.kind != "text" or len(block.text) <= _BLOCK_SPLIT_CHARS:
            out.append(block)
            continue
        buf = ""
        for line in block.text.split("\n"):
            while len(line) > _BLOCK_SPLIT_CHARS:
                if buf.strip():
                    out.append(replace(block, text=buf))
                buf = ""
                out.append(replace(block, text=line[:_BLOCK_SPLIT_CHARS]))
                line = line[_BLOCK_SPLIT_CHARS:]
            candidate = f"{buf}\n{line}" if buf else line
            if buf and len(candidate) > _BLOCK_SPLIT_CHARS:
                out.append(replace(block, text=buf))
                buf = line
            else:
                buf = candidate
        if buf.strip():
            out.append(replace(block, text=buf))
    return out


def build_local_document(blocks: list[LocalBlock]) -> ParsedDocument:
    """把阅读顺序的块装配成 ParsedDocument(locator_kind='section',bbox 占位)。

    与 content_list.build_document 同约定:标题更新 section 栈且自身也落段落;
    图注文本必须落进段落(图文关联按"图注文本出现在块内"判定 caption_of)。
    超长文本块先按行拆开再分页(见 _split_oversize)。
    """
    blocks = _split_oversize(blocks)
    numbers = paginate(blocks)
    page_count = max(numbers) if numbers else 0
    pages = [ParsedPage(page_number=i + 1, width=PAGE_W, height=PAGE_H, is_scanned=False)
             for i in range(page_count)]
    headings: dict[int, str] = {}
    counters = {"para": 0, "cap": 0, "fig": 0}
    placeholder = (0.0, 0.0, PAGE_W, PAGE_H)

    def section() -> str | None:
        return " > ".join(headings[k] for k in sorted(headings)) or None

    def add_paragraph(page: ParsedPage, text: str) -> None:
        text = text.strip()
        if not text:
            return
        counters["para"] += 1
        page.paragraphs.append(ParsedParagraph(
            id=f"p{page.page_number}_c{counters['para']}", text=text,
            page_number=page.page_number, bbox=placeholder, section=section(),
        ))

    for block, seg in zip(blocks, numbers):
        page = pages[seg - 1]
        if block.kind == "heading":
            if block.level <= SECTION_MAX_LEVEL:
                for lv in [k for k in headings if k >= block.level]:
                    del headings[lv]
                headings[block.level] = block.text
            add_paragraph(page, block.text)
        elif block.kind == "image" and block.image_bytes:
            caption_id = None
            caption_text = block.caption or block.text
            if block.caption:
                counters["cap"] += 1
                caption_id = f"p{seg}_cap{counters['cap']}"
                number, cap_text = match_caption(block.caption)
                page.captions.append(ParsedCaption(
                    id=caption_id, text=cap_text, figure_number=number,
                    page_number=seg, bbox=placeholder,
                ))
            add_paragraph(page, caption_text)
            counters["fig"] += 1
            page.figures.append(ParsedFigure(
                id=f"p{seg}_fig{counters['fig']}", page_number=seg, bbox=placeholder,
                kind=KIND_EMBEDDED_BITMAP, image_bytes=block.image_bytes,
                caption_id=caption_id,
            ))
        else:    # text,或没有字节的 image 块(只留 alt 文本)
            add_paragraph(page, block.text)
    return ParsedDocument(page_count=page_count, pages=pages, locator_kind="section")
