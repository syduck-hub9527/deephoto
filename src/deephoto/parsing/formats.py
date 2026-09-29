"""文档格式识别:魔数为准,扩展名必须与之相容;不一致直接拒绝,不猜。

判定顺序(见 DEV_multi_format_documents.md §3.1):
  %PDF → pdf;PK\\x03\\x04 且合法 zip → 看条目定 docx/pptx/xlsx;
  CFB(D0CF11E0) → 旧版 Office(对 .docx 等扩展名报"加密或损坏");
  图片魔数 → image;无魔数按扩展名走文本类(md/txt/html,校验可解码)。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

# locator_kind: page|slide|sheet|section(见 §3.2;page=真实页码,section=虚拟分段号)
_FORMATS = {
    "pdf":  ("pdf",  "application/pdf", "page"),
    "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "section"),
    "doc":  ("doc",  "application/msword", "page"),
    "pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "slide"),
    "ppt":  ("ppt",  "application/vnd.ms-powerpoint", "page"),
    "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "sheet"),
    "xls":  ("xls",  "application/vnd.ms-excel", "page"),
    "md":   ("md",   "text/markdown", "section"),
    "txt":  ("txt",  "text/plain", "section"),
    "html": ("html", "text/html", "section"),
}

# zip 条目 → OOXML 格式
_OOXML_MARKER = {
    "word/document.xml": "docx",
    "ppt/presentation.xml": "pptx",
    "xl/workbook.xml": "xlsx",
}
_CFB_EXTS = {"doc": "doc", "ppt": "ppt", "xls": "xls"}
_CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")

_IMAGE_MAGICS = [
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF8", "gif", "image/gif"),
    (b"BM", "bmp", "image/bmp"),
    (b"\x00\x00\x00\x0cjP  \r\n\x87\n", "jp2", "image/jp2"),
]
_IMAGE_EXTS = {"png", "jpg", "jpeg", "jp2", "webp", "gif", "bmp"}
_TEXT_EXTS = {"md": "md", "markdown": "md", "txt": "txt", "html": "html", "htm": "html"}


@dataclass(frozen=True)
class FormatInfo:
    key: str           # pdf|docx|doc|pptx|ppt|xlsx|xls|md|txt|html|image
    ext: str           # 存储用扩展名,如 "docx"
    mime: str
    locator_kind: str  # page|slide|sheet|section(引擎可覆盖个别情况)


class UnsupportedFormat(Exception):
    """格式识别失败。code: empty|mismatch|encrypted|corrupt|binary|undecodable|unsupported。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _info(key: str) -> FormatInfo:
    ext, mime, locator = _FORMATS[key]
    return FormatInfo(key=key, ext=ext, mime=mime, locator_kind=locator)


def supported_keys() -> list[str]:
    return [*_FORMATS, "image"]


def format_by_key(key: str) -> FormatInfo:
    """按 key 取 FormatInfo(入库时按文档记录的 source_format 还原)。"""
    if key == "image":
        return FormatInfo(key="image", ext="png", mime="image/png", locator_kind="page")
    if key not in _FORMATS:
        raise UnsupportedFormat("unsupported", f"未知的格式标识: {key}")
    return _info(key)


def decode_text(data: bytes) -> str:
    """文本类解码:utf-8-sig → gb18030;都失败抛 UnsupportedFormat。检测与解析共用同一顺序。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnsupportedFormat("undecodable", "文本编码无法识别(支持 UTF-8 / GB18030)")


def _ext_of(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem, dot, suffix = name.rpartition(".")
    return suffix.lower() if dot and stem else ""


def _detect_ooxml(data: bytes) -> str | None:
    """合法 zip 则按条目判定 docx/pptx/xlsx;无法识别返回 None;损坏抛 corrupt。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat("corrupt", "zip 容器损坏,无法读取") from exc
    for marker, key in _OOXML_MARKER.items():
        if marker in names:
            return key
    return None


def detect_format(data: bytes, filename: str) -> FormatInfo:
    """识别上传文件的格式;失败抛 UnsupportedFormat(原因可读,供 400/415 使用)。"""
    ext = _ext_of(filename or "")
    if not data:
        raise UnsupportedFormat("empty", "空文件")

    if data.startswith(b"%PDF"):
        if ext != "pdf":
            raise UnsupportedFormat("mismatch", "内容是 PDF,但扩展名不是 .pdf")
        return _info("pdf")

    if data.startswith(b"PK\x03\x04"):
        key = _detect_ooxml(data)
        if key is None:
            raise UnsupportedFormat("unsupported", "zip 容器但不是支持的 Office 格式(docx/pptx/xlsx)")
        if ext != key:
            raise UnsupportedFormat("mismatch", f"内容是 {key},但扩展名不是 .{key}")
        return _info(key)

    if data.startswith(_CFB_MAGIC):
        if ext in _CFB_EXTS:
            return _info(_CFB_EXTS[ext])
        if ext in ("docx", "pptx", "xlsx"):
            raise UnsupportedFormat("encrypted", "文件已加密或损坏(加密的 Office 文档请先解密再上传)")
        raise UnsupportedFormat("mismatch", "内容是旧版 Office 复合文档,但扩展名不是 .doc/.ppt/.xls")

    for magic, img_ext, mime in _IMAGE_MAGICS:
        if data.startswith(magic):
            if ext and ext not in _IMAGE_EXTS:
                raise UnsupportedFormat("mismatch", f"内容是图片,但扩展名 .{ext} 不是图片扩展名")
            return FormatInfo(key="image", ext=img_ext, mime=mime, locator_kind="page")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if ext and ext not in _IMAGE_EXTS:
            raise UnsupportedFormat("mismatch", f"内容是图片,但扩展名 .{ext} 不是图片扩展名")
        return FormatInfo(key="image", ext="webp", mime="image/webp", locator_kind="page")

    if ext in _TEXT_EXTS:
        if b"\x00" in data:
            raise UnsupportedFormat("binary", "扩展名是文本类,但内容含 NUL 字节,疑似二进制文件")
        decode_text(data)   # 只验证可解码;解析时重新解码
        return _info(_TEXT_EXTS[ext])

    raise UnsupportedFormat(
        "unsupported",
        f"不支持的格式(.{ext or '无扩展名'});支持: " + "/".join(supported_keys()))
