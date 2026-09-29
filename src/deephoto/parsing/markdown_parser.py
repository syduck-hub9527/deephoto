"""Markdown 本地解析(MinerU 不支持 md;DEV_multi_format_documents.md §3.4d)。

要点:
- 先剥离 YAML front matter(否则 --- 被解析成 hr + h2,污染 section);
- 表格/围栏代码/列表保留原始 markdown 文本作为段落(检索友好,不丢结构);
- 图片:data: URI 解码入库(单张上限可配);http(s) 永不抓取(SSRF/隐私);
  相对路径只在"随包上传"(P4)才可解析,现在保留 alt 并告警;
- HTML 块去标签留文本,<script>/<style> 内容丢弃。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from html.parser import HTMLParser

from ..config import Settings
from .captions import match_caption
from .formats import decode_text
from .paginate import LocalBlock, build_local_document
from .registry import SourceFile

logger = logging.getLogger(__name__)

_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_DATA_URI_RE = re.compile(r"\Adata:([\w/+.\-]+);base64,(.*)\Z", re.DOTALL)
_REMOTE_RE = re.compile(r"\Ahttps?://", re.IGNORECASE)


def _strip_front_matter(text: str) -> str:
    return _FRONT_MATTER_RE.sub("", text, count=1)


class _HtmlTextExtractor(HTMLParser):
    """HTML 块去标签:script/style 内容丢弃;块级标签补换行。"""

    _SKIP = {"script", "style"}
    _BLOCK = {"p", "div", "br", "li", "tr", "table", "section", "article", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip("\n")


def html_block_text(raw: str) -> str:
    parser = _HtmlTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw).strip()
    return parser.text()


class MarkdownParser:
    """Markdown -> ParsedDocument(locator_kind='section')。纯本地,无网络。"""

    engine = "local"

    def __init__(self, settings: Settings):
        self._settings = settings

    def parse(self, src: SourceFile, observer) -> "object":
        from markdown_it import MarkdownIt   # 懒导入:纯逻辑测试不强制加载

        text = decode_text(src.data).replace("\r\n", "\n").replace("\r", "\n")
        text = _strip_front_matter(text)
        md = MarkdownIt("commonmark").enable("table")
        tokens = md.parse(text)
        lines = text.split("\n")
        blocks = self._blocks_from_tokens(tokens, lines, observer)
        self._pair_captions(blocks)
        return build_local_document(blocks)

    # ---- token 遍历 ----

    def _blocks_from_tokens(self, tokens, lines: list[str], observer) -> list[LocalBlock]:
        blocks: list[LocalBlock] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.level != 0:       # 只走顶层块;嵌套内容由各自的 open/close 区间处理
                i += 1
                continue
            ttype = tok.type
            if ttype == "heading_open":
                inline = tokens[i + 1] if i + 1 < len(tokens) else None
                content = (inline.content if inline is not None and inline.type == "inline" else "").strip()
                if content:
                    blocks.append(LocalBlock("heading", text=content, level=int(tok.tag[1:])))
                i += 3               # heading_open / inline / heading_close
                continue
            if ttype == "paragraph_open":
                inline = tokens[i + 1] if i + 1 < len(tokens) else None
                if inline is not None and inline.type == "inline":
                    self._emit_inline(inline, blocks, observer)
                i += 3               # paragraph_open / inline / paragraph_close
                continue
            if ttype in ("fence", "code_block"):
                raw = self._slice(lines, tok.map)
                if raw.strip():
                    blocks.append(LocalBlock("text", text=raw))
                i += 1
                continue
            if ttype in ("table_open", "bullet_list_open", "ordered_list_open", "blockquote_open"):
                close_type = ttype.replace("_open", "_close")
                depth = 1
                j = i + 1
                while j < len(tokens):
                    if tokens[j].type == ttype:
                        depth += 1
                    elif tokens[j].type == close_type:
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                end = tokens[j].map[1] if j < len(tokens) and tokens[j].map else \
                    (tok.map[1] if tok.map else None)
                raw = self._slice(lines, (tok.map[0], end)) if tok.map and end is not None else ""
                if raw.strip():
                    blocks.append(LocalBlock("text", text=raw))
                i = j + 1
                continue
            if ttype == "html_block":
                body = html_block_text(self._slice(lines, tok.map) or tok.content)
                if body.strip():
                    blocks.append(LocalBlock("text", text=body))
                i += 1
                continue
            # hr / 其他顶层 token:忽略
            i += 1
        return blocks

    def _emit_inline(self, inline, blocks: list[LocalBlock], observer) -> None:
        """段落 inline:文本与图片按出现顺序拆块;图片语法不留在正文文本里。"""
        buf: list[str] = []
        for child in inline.children or []:
            if child.type == "image":
                if "".join(buf).strip():
                    blocks.append(LocalBlock("text", text="".join(buf)))
                buf = []
                blocks.append(self._image_block(child, observer))
            elif child.type == "code_inline":
                buf.append(f"`{child.content}`")
            elif child.type in ("text", "html_inline"):
                buf.append(child.content)
            elif child.type == "softbreak":
                buf.append("\n")
        if "".join(buf).strip():
            blocks.append(LocalBlock("text", text="".join(buf)))

    def _image_block(self, token, observer) -> LocalBlock:
        src = str(dict(token.attrs or {}).get("src") or "")
        alt = (token.content or "").strip()
        if src.startswith("data:"):
            m = _DATA_URI_RE.match(src)
            data = None
            if m:
                try:
                    data = base64.b64decode(m.group(2), validate=False)
                except (binascii.Error, ValueError):
                    data = None
            limit = self._settings.markdown_data_uri_max_mb * 1024 * 1024
            if data and len(data) <= limit and self._decodable(data):
                return LocalBlock("image", text=alt, image_bytes=data)
            reason = "超过内联上限" if data and len(data) > limit else "无法解码"
            observer.warn(f"内嵌图片{reason},已跳过(仅保留 alt 文本)")
            return LocalBlock("text", text=alt)
        if _REMOTE_RE.match(src):
            # 远程图:永不抓取(SSRF/隐私),alt 文本入段落
            return LocalBlock("text", text=alt)
        # 相对路径:随包上传(P4)才能解析
        observer.warn(f"图片文件未随文档上传({src[:80]}),已跳过(仅保留 alt 文本)")
        return LocalBlock("text", text=alt)

    @staticmethod
    def _decodable(data: bytes) -> bool:
        from io import BytesIO
        from PIL import Image
        try:
            with Image.open(BytesIO(data)) as im:
                im.verify()
            return True
        except Exception:
            return False

    @staticmethod
    def _slice(lines: list[str], span) -> str:
        if not span:
            return ""
        start, end = span
        return "\n".join(lines[start:end])

    @staticmethod
    def _pair_captions(blocks: list[LocalBlock]) -> None:
        """图注配对:alt 命中图注格式优先;否则看图后紧邻的文字块(复用 match_caption)。"""
        for index, block in enumerate(blocks):
            if block.kind != "image" or not block.image_bytes:
                continue
            if block.text and match_caption(block.text)[0]:
                block.caption = block.text
                continue
            nxt = blocks[index + 1] if index + 1 < len(blocks) else None
            if nxt is not None and nxt.kind == "text" and match_caption(nxt.text)[0]:
                block.caption = nxt.text
