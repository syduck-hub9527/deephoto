"""图文关联:显式证据优先,位置推断兜底(开发文档§3.3)。

关系与置信度:
- references(0.95):正文显式“见图 N”;
- caption_of(1.0):图注文本落在该块内(解析器已完成图注-图形配对);
- whole_document(1.0):整图文档(source_format='image')——图即全文,
  唯一的图与该文档所有文本块强制关联(见 whole_document_links);
- nearby(0.4):同页/页码范围内的候选,仅作低置信候选,不自动作为确定证据展示。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LinkDraft:
    chunk_id: str
    image_occurrence_id: str
    relation: str        # references|caption_of|whole_document|nearby
    confidence: float

CONF_REFERENCE = 0.95
CONF_CAPTION = 1.0
CONF_WHOLE_DOCUMENT = 1.0
CONF_NEARBY = 0.4
# 回答中可作为确定证据展示的最低置信度(开发文档§3.3.5)
CONF_DISPLAY_THRESHOLD = 0.8


def resolve_links(
    chunks: list[dict],        # {id, text, page_start, page_end, referenced_image_ids}
    occurrences: list[dict],   # {id, page_number, figure_number, caption}
) -> list[LinkDraft]:
    links: dict[tuple[str, str, str], LinkDraft] = {}

    def add(chunk_id: str, occ_id: str, relation: str, confidence: float) -> None:
        key = (chunk_id, occ_id, relation)
        if key not in links:
            links[key] = LinkDraft(chunk_id, occ_id, relation, confidence)

    for chunk in chunks:
        chunk_pages = range(chunk["page_start"], chunk["page_end"] + 1)
        # 显式引用
        for occ_id in chunk.get("referenced_image_ids", []):
            add(chunk["id"], occ_id, "references", CONF_REFERENCE)
        for occ in occurrences:
            # 图注落在块内
            caption = (occ.get("caption") or "").strip()
            if caption and caption[:30] in chunk["text"]:
                add(chunk["id"], occ["id"], "caption_of", CONF_CAPTION)
                continue
            # 位置邻近(且未被更强关系覆盖)
            if occ["page_number"] in chunk_pages:
                add(chunk["id"], occ["id"], "nearby", CONF_NEARBY)
    return list(links.values())


def whole_document_links(chunks: list[dict], occurrences: list[dict]) -> list[LinkDraft]:
    """整图文档(source_format='image'):唯一的图与所有文本块强制关联(1.0)。

    MinerU 对图片输入只回 OCR 文本(样本实测无 image 元素),图与 OCR 文本块之间
    只有 nearby(0.4),够不到展示阈值——用户问"图里写了什么"时拿到文字却看不到
    原图。图片文档的图就是全文本身,关联是确定证据而非邻近候选。
    """
    return [LinkDraft(chunk["id"], occ["id"], "whole_document", CONF_WHOLE_DOCUMENT)
            for occ in occurrences for chunk in chunks]
