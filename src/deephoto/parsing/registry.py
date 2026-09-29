"""解析器注册表:按格式与配置选择引擎,产出统一的 ParsedDocument。

下游(分块/图文关联/索引/问答)只依赖 ParsedDocument,与具体引擎解耦。
引擎选择:PDF/旧版 Office→MinerU;md/txt/docx/pptx→本地(可切云端);
图片→MinerU OCR,无 Token 且开了描述模型时降级为本地整图入库(§3.4f);
xlsx/html 在后续阶段落地,现在给出可读原因而不是默默失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import Settings
from .base import ParsedDocument
from .formats import FormatInfo


@dataclass
class SourceFile:
    data: bytes
    filename: str
    fmt: FormatInfo


class EngineUnavailable(RuntimeError):
    """该格式当前没有可用解析引擎;消息是可读原因,直接进入库失败路径。"""


class ParseValidationError(ValueError):
    """解析结果违反不变量(页码/id/空文档);消息是可读原因。"""


class DocumentParser(Protocol):
    """统一解析协议:任何格式都产出 ParsedDocument(结构不变,位置语义由 locator_kind 决定)。"""

    engine: str                     # "mineru" | "local"

    def parse(self, src: SourceFile, observer) -> ParsedDocument: ...


_UNAVAILABLE = {
    "xlsx": "Excel(xlsx)本地解析器将在后续版本提供",
    "html": "HTML 需要 MinerU-HTML 引擎,将在后续版本提供",
}


def engine_for(fmt: FormatInfo, settings: Settings) -> str:
    """格式的解析引擎("mineru"|"local");无可用引擎抛 EngineUnavailable(可读原因)。"""
    if fmt.key == "pdf":
        return "mineru"
    if fmt.key in ("md", "txt"):
        return "local"
    if fmt.key == "docx":
        return settings.docx_parser     # local(默认)| mineru(切云端需 Token)
    if fmt.key == "pptx":
        return settings.pptx_parser     # local(默认)| mineru(切云端需 Token)
    if fmt.key in ("doc", "ppt", "xls"):
        # 旧版 Office(CFB 容器)无本地解析器,只能 MinerU 云端
        if not settings.mineru_api_key:
            raise EngineUnavailable(
                f"旧版 Office(.{fmt.key})需要 MinerU 云端解析:请配置 DEEPHOTO_MINERU_API_KEY")
        return "mineru"
    if fmt.key == "image":
        if settings.mineru_api_key:
            return "mineru"             # OCR(默认)
        if settings.description_enabled:
            return "local"              # 降级:整图入库,靠描述模型检索(§3.4f)
        raise EngineUnavailable(
            "图片文件需要 MinerU OCR(配置 DEEPHOTO_MINERU_API_KEY)"
            "或图片描述模型(配置 DEEPHOTO_DESCRIPTION_* 并启用 DESCRIPTION_ENABLED)")
    raise EngineUnavailable(_UNAVAILABLE.get(fmt.key, f"暂不支持 {fmt.key} 格式"))


def create_parser(fmt: FormatInfo, settings: Settings, *, engine: str | None = None,
                  mineru_client_factory=None) -> DocumentParser:
    """构造解析器。engine 缺省按配置解析;入库时以文档记录的 parse_engine 为准
    (上传与入库之间的配置变化不改变已排队文档的引擎)。懒导入,避免纯逻辑测试加载重依赖。
    """
    engine = engine or engine_for(fmt, settings)
    if engine == "mineru":
        from .mineru_parser import MinerUParser
        return MinerUParser(settings, client_factory=mineru_client_factory)
    if fmt.key == "md":
        from .markdown_parser import MarkdownParser
        return MarkdownParser(settings)
    if fmt.key == "txt":
        from .text_parser import TextParser
        return TextParser(settings)
    if fmt.key == "docx":
        from .docx_parser import DocxParser
        return DocxParser(settings)
    if fmt.key == "pptx":
        from .pptx_parser import PptxParser
        return PptxParser(settings)
    if fmt.key == "image":
        from .image_parser import ImageParser
        return ImageParser(settings)
    raise EngineUnavailable(_UNAVAILABLE.get(fmt.key, f"暂不支持 {fmt.key} 格式"))


def validate_parsed(parsed: ParsedDocument) -> None:
    """解析结果不变量(§3.3);违反抛 ParseValidationError(可读原因),不静默 ready 出空文档。

    图字节可解码性由入库处 _persist_figures 跳过+告警处理(与既有行为一致),不在此拒绝整篇。
    """
    if parsed.page_count < 1 or len(parsed.pages) != parsed.page_count:
        raise ParseValidationError(
            f"解析结果页数异常(page_count={parsed.page_count},实际 {len(parsed.pages)} 页)")
    ids: set[str] = set()
    has_text = False
    has_figure = False
    for expect, page in enumerate(parsed.pages, start=1):
        if page.page_number != expect:
            raise ParseValidationError(
                f"解析结果页码不连续(期望第 {expect} 页,实际 {page.page_number})")
        for group in (page.paragraphs, page.captions, page.figures):
            for item in group:
                if not 1 <= item.page_number <= parsed.page_count:
                    raise ParseValidationError(
                        f"元素 {item.id} 页码 {item.page_number} 超出范围(共 {parsed.page_count} 页)")
                if item.id in ids:
                    raise ParseValidationError(f"元素 id 重复: {item.id}")
                ids.add(item.id)
        if any(p.text.strip() for p in page.paragraphs):
            has_text = True
        if page.figures:
            has_figure = True
    if not has_text and not has_figure:
        raise ParseValidationError("未解析出任何文字或图片(文件可能为空、加密或全部为不支持的对象)")
