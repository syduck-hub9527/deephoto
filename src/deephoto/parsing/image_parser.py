"""图片文件的本地降级解析(DEV_multi_format_documents.md §3.4f)。

无 MinerU Token 时的兜底:整图作为唯一 figure(1 页、无正文),不 OCR;
仍可靠"图片描述"被检索(需 DESCRIPTION_ENABLED,由 registry 在引擎选择时保证)。
有 Token 时图片走 MinerU OCR,不经此解析器。
"""

from __future__ import annotations

import io

from ..config import Settings
from .base import KIND_EMBEDDED_BITMAP, ParsedDocument, ParsedFigure, ParsedPage
from .formats import UnsupportedFormat
from .registry import SourceFile


class ImageParser:
    """单张图片 -> ParsedDocument(1 页 1 图,无正文)。纯本地,无网络。"""

    engine = "local"

    def __init__(self, settings: Settings):
        self._settings = settings

    def parse(self, src: SourceFile, observer):
        from PIL import Image

        try:
            with Image.open(io.BytesIO(src.data)) as img:
                width, height = img.size
        except Exception as exc:     # 魔数已过但解码失败(损坏/截断):可读原因
            raise UnsupportedFormat("corrupt", f"图片无法解码: {exc}") from exc

        observer.warn("未配置 MinerU Token,图片未做 OCR:整图入库,仅靠图片描述检索")
        page = ParsedPage(page_number=1, width=float(width), height=float(height),
                          is_scanned=False)
        page.figures.append(ParsedFigure(
            id="p1_fig1", page_number=1, bbox=(0.0, 0.0, float(width), float(height)),
            kind=KIND_EMBEDDED_BITMAP, image_bytes=src.data,
        ))
        return ParsedDocument(page_count=1, pages=[page], locator_kind="page")
