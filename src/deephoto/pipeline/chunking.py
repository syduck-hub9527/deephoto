"""正文分块:按章节与语义边界切段(纯函数)。

对应开发文档§3.5“正文索引”:每块保留章节、页码范围、段落 ID、
显式引用的图号与邻近图号(入库时解析为 ImageOccurrence id)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from ..parsing.base import ParsedParagraph
from ..parsing.captions import find_referenced_figures

TARGET_CHARS = 900     # 目标块长;超长段落按此长度拆分
MAX_CHARS = 1300       # 块长度硬上限:任何块的文本都不会超过它
MIN_CHARS = 250        # 低于此长度的尾块尽量并入前一块

# 句末切分点:中文句号/问号/叹号/分号/换行之后,或英文句点+空白之后
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;\n])|(?<=\.)(?=\s)")


@dataclass
class ChunkDraft:
    section: str | None
    text: str
    page_start: int
    page_end: int
    paragraph_ids: list[str] = field(default_factory=list)
    referenced_figures: list[str] = field(default_factory=list)   # 图号,如 "3"


def split_long_text(text: str, limit: int = TARGET_CHARS) -> list[str]:
    """把超长文本按句子边界拆成不超过 limit 的片段;无标点的超长句硬切。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces: list[str] = []
    buf = ""
    for sentence in filter(None, _SENTENCE_END.split(text)):
        while len(sentence) > limit:          # 单句就超长:先收尾 buf,再硬切
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if buf and len(buf) + len(sentence) > limit:
            pieces.append(buf)
            buf = ""
        buf += sentence
    if buf:
        pieces.append(buf)
    return [piece.strip() for piece in pieces if piece.strip()]


def _expand_oversize(paragraphs: list[ParsedParagraph]) -> list[ParsedParagraph]:
    """超过 MAX_CHARS 的段落(如 MinerU 整页文本)拆成多个子段落,保证块有硬上限。"""
    expanded: list[ParsedParagraph] = []
    for para in paragraphs:
        text = para.text.strip()
        if len(text) <= MAX_CHARS:
            expanded.append(para)
            continue
        for index, piece in enumerate(split_long_text(text), start=1):
            expanded.append(replace(para, id=f"{para.id}#{index}", text=piece))
    return expanded


def chunk_paragraphs(paragraphs: list[ParsedParagraph]) -> list[ChunkDraft]:
    """输入按阅读顺序排列的段落,输出分块草稿。每块文本长度 <= MAX_CHARS。"""
    paragraphs = _expand_oversize(paragraphs)
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
        if would_exceed or section_changed or page_gap:
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
