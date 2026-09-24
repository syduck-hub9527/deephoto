"""pdf_backend(poppler)测试:有 poppler 时用合成 PDF 真跑,否则跳过。"""

import unittest

import _bootstrap  # noqa: F401
from deephoto.parsing import pdf_backend
from deephoto.parsing.pymupdf_parser import PopplerParser


def _make_pdf(page_texts):
    """最小多页文字 PDF(每页一行文本)。"""
    objects = []
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(page_texts)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode())
    font_obj_num = 3 + len(page_texts) * 2
    for i, text in enumerate(page_texts):
        content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
        page_num = 3 + i * 2
        content_num = page_num + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_num} 0 R /Resources << /Font << /F1 {font_obj_num} 0 R >> >> >>".encode())
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for num, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (num, body)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n" % (len(objects) + 1)
    pdf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref)
    return bytes(pdf)


@unittest.skipUnless(pdf_backend.poppler_available(), "需要 poppler(pdftotext/pdftoppm)")
class PDFBackendTest(unittest.TestCase):
    def test_page_count(self):
        pdf = _make_pdf(["one", "two", "three"])
        self.assertEqual(pdf_backend.page_count(pdf), 3)

    def test_page_texts(self):
        pdf = _make_pdf(["alpha", "beta"])
        texts = pdf_backend.page_texts(pdf)
        self.assertEqual(len(texts), 2)
        self.assertIn("alpha", texts[0])
        self.assertIn("beta", texts[1])

    def test_render_page_returns_png(self):
        pdf = _make_pdf(["hello"])
        png = pdf_backend.render_page(pdf, 1)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_parser_classifies_text_and_scanned_pages(self):
        # 有文字的页 -> 文字版;空白页 -> 扫描页(整页回退图)
        pdf = _make_pdf(["这是一些有内容的文字 hello world", ""])
        parsed = PopplerParser().parse(pdf)
        self.assertEqual(parsed.page_count, 2)
        self.assertFalse(parsed.pages[0].is_scanned)
        self.assertTrue(parsed.pages[1].is_scanned)
        self.assertEqual(len(parsed.pages[1].figures), 1)


if __name__ == "__main__":
    unittest.main()
