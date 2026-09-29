"""纯文本(.txt)本地解析:空行分段,无标题,虚拟分页(§3.4e)。"""

from __future__ import annotations

import re

from ..config import Settings
from .formats import decode_text
from .paginate import LocalBlock, build_local_document
from .registry import SourceFile

_BLANK_RE = re.compile(r"\n[ \t]*\n")


class TextParser:
    """txt -> ParsedDocument(locator_kind='section')。纯本地,无网络。"""

    engine = "local"

    def __init__(self, settings: Settings):
        self._settings = settings

    def parse(self, src: SourceFile, observer) -> "object":
        text = decode_text(src.data).replace("\r\n", "\n").replace("\r", "\n")
        blocks = [LocalBlock("text", text=para.strip())
                  for para in _BLANK_RE.split(text) if para.strip()]
        return build_local_document(blocks)
