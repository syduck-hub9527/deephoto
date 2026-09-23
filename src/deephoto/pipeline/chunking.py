"""正文分块:按章节与语义边界切段(纯函数)。

对应开发文档§3.5“正文索引”:每块保留章节、页码范围、段落 ID、
显式引用的图号与邻近图号(入库时解析为 ImageOccurrence id)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..parsing.base import ParsedParagraph
from ..parsing.captions import find_referenced_figures

TARGET_CHARS = 900     # 目标块长
MAX_CHARS = 1300       # 超过即强制切分(在段落边界)
MIN_CHARS = 250        # 低于此长度尽量与下一段合并


@dataclass
class ChunkDraft:
    section: str | None
    text: str
    page_start: int
    page_end: int
    paragraph_ids: list[str] = field(default_factory=list)
    referenced_figures: list[str] = field(default_factory=list)   # 图号,如 "3"


def chunk_paragraphs(paragraphs: list[ParsedParagraph]) -> list[ChunkDraft]:
    """输入按阅读顺序排列的段落,输出分块草稿。"""
    chunks: list[ChunkDraft] = []
    current: ChunkDraft | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current.text.strip():
            chunks.append(current)
        current = None

    for para in paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if current is None:
            current = ChunkDraft(
                section=para.section, text=text,
                page_start=para.page_number, page_end=para.page_number,
                paragraph_ids=[para.id],
                referenced_figures=find_referenced_figures(text),
            )
            continue
        would_exceed = len(current.text) + len(text) + 1 > MAX_CHARS
        big_enough = len(current.text) >= MIN_CHARS
        section_changed = para.section != current.section   # 章节是硬边界,始终切分
        page_gap = para.page_number > current.page_end + 1 and big_enough
        if (would_exceed and big_enough) or section_changed or page_gap:
            flush()
            current = ChunkDraft(
                section=para.section, text=text,
                page_start=para.page_number, page_end=para.page_number,
                paragraph_ids=[para.id],
                referenced_figures=find_referenced_figures(text),
            )
        else:
            current.text += "\n" + text
            current.page_end = max(current.page_end, para.page_number)
            current.paragraph_ids.append(para.id)
            for num in find_referenced_figures(text):
                if num not in current.referenced_figures:
                    current.referenced_figures.append(num)
    flush()

    # 过短的尾块并入前一块(同章节时)
    merged: list[ChunkDraft] = []
    for chunk in chunks:
        if (
            merged and len(chunk.text) < MIN_CHARS
            and chunk.section == merged[-1].section
            and len(merged[-1].text) + len(chunk.text) + 1 <= MAX_CHARS
        ):
            prev = merged[-1]
            prev.text += "\n" + chunk.text
            prev.page_end = max(prev.page_end, chunk.page_end)
            prev.paragraph_ids.extend(chunk.paragraph_ids)
            for num in chunk.referenced_figures:
                if num not in prev.referenced_figures:
                    prev.referenced_figures.append(num)
        else:
            merged.append(chunk)
    return merged
