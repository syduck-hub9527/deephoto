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

_FRONT_MATTER_MAX_LINES = 100        # 闭合 --- 必须在前 100 行内,否则视为普通分隔线
_YAML_KEY_RE = re.compile(r"^[\w.-]+\s*:")   # front matter 正文第一行非空内容须像 YAML 键
_DATA_URI_RE = re.compile(r"\Adata:([\w/+.\-]+);base64,(.*)\Z", re.DOTALL)
_REMOTE_RE = re.compile(r"\Ahttps?://", re.IGNORECASE)
# 容器块(列表/表格/引用)整块切原文后,把 ![alt](...) 换成 alt;
# data URI 的 base64 长串随之抹掉,不污染检索与分块
_IMG_SYNTAX_RE = re.compile(r"!\[([^\]]*)\]\(\s*[^)\s]+(?:\s+\"[^\"]*\")?\s*\)")
# 行内 HTML 去标签:<img> 保留 alt 文本,其余属性(含 src="data:..." 长串)随标签抹掉。
# 只匹配真正的标签:已知标签名 + 合法属性语法(属性名须 ASCII 字母/_/: 开头,值可引号);
# std::vector<int>、a<b 且 c>d、Map<K,V>、<https://...> 都不是标签,原样保留
_KNOWN_TAGS = (
    "img", "br", "hr", "a", "b", "i", "em", "strong", "code", "span", "sup", "sub",
    "u", "s", "del", "ins", "mark", "small", "kbd", "samp", "var", "abbr", "cite", "q",
    "table", "thead", "tbody", "tr", "td", "th", "ul", "ol", "li", "dl", "dt", "dd",
    "p", "div", "section", "article", "figure", "figcaption", "details", "summary",
    "font", "center", "h1", "h2", "h3", "h4", "h5", "h6",
)
_TAG_ATTR = r"(?:\s+[a-zA-Z_:][\w:.-]*(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?)*"
_HTML_TAG_RE = re.compile(r"</?(?:" + "|".join(_KNOWN_TAGS) + r")" + _TAG_ATTR + r"\s*/?>",
                          re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img" + _TAG_ATTR + r"\s*/?>", re.IGNORECASE)
_IMG_ALT_RE = re.compile(r"\balt\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.IGNORECASE)
_CONTAINER_CODE_RE = re.compile(r"`[^`\n]*`")   # 容器原文里的行内代码(示例文本,不清理)
# 成对的 <script>/<style> 整块(标签+内容)移除,与顶层 HTML 块语义一致
# (顶层由 _HtmlTextExtractor 丢弃其内容);不配对的孤立标签由白名单规则原样保留
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\s*\1\s*>", re.IGNORECASE | re.DOTALL)


def _strip_front_matter(text: str) -> str:
    """剥离 YAML front matter。防线:首行 --- 的闭合 --- 须在前 100 行内,
    且其间第一行非空内容须像 YAML 键(key: 形式);否则是正文里的普通分隔线,不剥离。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, min(len(lines), _FRONT_MATTER_MAX_LINES + 1)):
        if lines[i].strip() == "---":
            first = next((l for l in lines[1:i] if l.strip()), "")
            if _YAML_KEY_RE.match(first):
                return "\n".join(lines[i + 1:])
            return text
    return text


def _strip_image_syntax(text: str) -> str:
    return _IMG_SYNTAX_RE.sub(lambda m: m.group(1), text)


def _strip_inline_html(text: str) -> str:
    """行内 HTML 去标签:<img> 保留 alt(双引号/单引号/无引号写法都认);其余已知标签
    整体移除(属性随标签,含 src="data:..." 长串)。非标签的尖括号内容原样保留。"""
    def img_alt(m: re.Match) -> str:
        alt = _IMG_ALT_RE.search(m.group(0))
        if not alt:
            return ""
        value = next((g for g in alt.groups() if g is not None), "")
        return value.strip()
    return _HTML_TAG_RE.sub("", _IMG_TAG_RE.sub(img_alt, text))


def _strip_container_markup(text: str) -> str:
    """容器块(列表/表格/引用)原文清理:成对的 <script>/<style> 整块移除(与顶层
    HTML 块一致);图片语法/行内 HTML 只留 alt;反引号行内代码里是示例文本,原样保留。"""

    def clean(part: str) -> str:
        return _strip_inline_html(_strip_image_syntax(_SCRIPT_STYLE_RE.sub("", part)))

    parts: list[str] = []
    last = 0
    for m in _CONTAINER_CODE_RE.finditer(text):
        if m.start() > last:
            parts.append(clean(text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(clean(text[last:]))
    return "".join(parts)


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
                # 容器内的图片语法/行内 HTML 只留 alt(真标签才动;行内代码原样);
                # 围栏代码原样保留仅对顶层围栏成立:容器内嵌套的围栏不做二次识别,
                # 其中若有示例图片语法也会被替换成 alt——只影响示例文本的展示
                raw = _strip_container_markup(raw)
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
            elif child.type == "text":
                buf.append(child.content)
            elif child.type == "html_inline":
                buf.append(_strip_inline_html(child.content))   # 去标签;<img> 只留 alt
            elif child.type in ("softbreak", "hardbreak"):
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
