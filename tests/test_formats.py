"""格式嗅探:魔数为准、扩展名相容、不一致拒绝(DEV_multi_format_documents.md §8)。"""

import io
import unittest
import zipfile

import _bootstrap  # noqa: F401

from deephoto.parsing.formats import UnsupportedFormat, detect_format


def _zip_with(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in names:
            z.writestr(name, "x")
    return buf.getvalue()


CFB = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 64


def _detect(data: bytes, name: str):
    return detect_format(data, name)


class DetectPositiveTest(unittest.TestCase):
    def test_pdf(self):
        fmt = _detect(b"%PDF-1.4 ...", "a.pdf")
        self.assertEqual((fmt.key, fmt.ext, fmt.locator_kind), ("pdf", "pdf", "page"))

    def test_ooxml_by_zip_entry(self):
        for names, key, locator in (
            (("word/document.xml",), "docx", "section"),
            (("ppt/presentation.xml",), "pptx", "slide"),
            (("xl/workbook.xml",), "xlsx", "sheet"),
        ):
            fmt = _detect(_zip_with(*names), f"a.{key}")
            self.assertEqual((fmt.key, fmt.locator_kind), (key, locator))

    def test_cfb_legacy_office(self):
        for ext in ("doc", "ppt", "xls"):
            self.assertEqual(_detect(CFB, f"a.{ext}").key, ext)

    def test_images(self):
        for magic, ext in ((b"\x89PNG\r\n\x1a\nrest", "png"), (b"\xff\xd8\xff\xe0xx", "jpg"),
                           (b"GIF89a...", "gif"), (b"BM....", "bmp"),
                           (b"\x00\x00\x00\x0cjP  \r\n\x87\nxx", "jp2"),
                           (b"RIFF\x00\x00\x00\x00WEBPxx", "webp")):
            fmt = _detect(magic, f"a.{ext}")
            self.assertEqual((fmt.key, fmt.ext, fmt.locator_kind), ("image", ext, "page"))

    def test_text_kinds(self):
        for ext, key in (("md", "md"), ("markdown", "md"), ("txt", "txt"),
                         ("html", "html"), ("htm", "html")):
            fmt = _detect("正文".encode(), f"a.{ext}")
            self.assertEqual((fmt.key, fmt.locator_kind), (key, "section"))

    def test_gbk_and_bom_text(self):
        self.assertEqual(_detect("标题".encode("gb18030"), "a.md").key, "md")
        self.assertEqual(_detect(b"\xef\xbb\xbf# t", "a.txt").key, "txt")

    def test_utf16_bom_text_accepted(self):
        # Windows 记事本"Unicode"(UTF-16 带 BOM):含大量 NUL 但不是二进制
        data = "# 标题\n\n正文".encode("utf-16")
        self.assertEqual(_detect(data, "a.md").key, "md")
        self.assertEqual(_detect(data, "a.txt").key, "txt")

    def test_pathy_filename_uses_basename_ext(self):
        self.assertEqual(_detect(b"%PDF-1.4", "../evil/a.pdf").key, "pdf")


class DetectRejectTest(unittest.TestCase):
    def _reject(self, code: str, data: bytes, name: str):
        with self.assertRaises(UnsupportedFormat) as ctx:
            detect_format(data, name)
        self.assertEqual(ctx.exception.code, code)

    def test_empty(self):
        self._reject("empty", b"", "a.pdf")

    def test_docx_ext_but_pdf_content(self):
        self._reject("mismatch", b"%PDF-1.4", "a.docx")

    def test_pdf_ext_but_docx_content(self):
        self._reject("mismatch", _zip_with("word/document.xml"), "a.pdf")

    def test_docx_ext_but_cfb_content_is_encrypted(self):
        self._reject("encrypted", CFB, "a.docx")

    def test_cfb_with_wrong_legacy_ext(self):
        self._reject("mismatch", CFB, "a.txt")

    def test_unknown_zip(self):
        self._reject("unsupported", _zip_with("META-INF/MANIFEST.MF"), "a.zip")

    def test_corrupt_zip(self):
        self._reject("corrupt", b"PK\x03\x04 broken", "a.docx")

    def test_nul_in_txt(self):
        self._reject("binary", b"ab\x00cd", "a.txt")

    def test_utf16_without_bom_rejected_as_binary(self):
        # 无 BOM 的 UTF-16 无法可靠识别(现状):按二进制拒绝
        self._reject("binary", "# 标题".encode("utf-16-le"), "a.md")

    def test_undecodable_text(self):
        self._reject("undecodable", b"\xff\xff\xff\xff\xfe", "a.md")

    def test_image_magic_with_non_image_ext(self):
        self._reject("mismatch", b"\x89PNG\r\n\x1a\nrest", "a.txt")

    def test_unknown_extension(self):
        self._reject("unsupported", b"hello", "a.xyz")

    def test_no_extension(self):
        self._reject("unsupported", b"hello", "README")


if __name__ == "__main__":
    unittest.main()
