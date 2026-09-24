"""IngestService 的 MineU 解析结果整理逻辑测试(不拉完整管线)。"""

import unittest

import _bootstrap  # noqa: F401
from deephoto.pipeline.ingest import _pages_from_texts


class PagesFromTextsTest(unittest.TestCase):
    def test_builds_one_page_paragraph_per_text(self):
        parsed = _pages_from_texts(["第一页内容", "第二页内容"])
        self.assertEqual(parsed.page_count, 2)
        self.assertEqual(len(parsed.pages), 2)
        self.assertEqual(parsed.pages[0].paragraphs[0].text, "第一页内容")
        self.assertEqual(parsed.pages[1].paragraphs[0].text, "第二页内容")
        # 页号从 1 起
        self.assertEqual(parsed.pages[0].page_number, 1)
        self.assertEqual(parsed.pages[1].page_number, 2)

    def test_empty_page_text_yields_page_without_paragraph(self):
        parsed = _pages_from_texts(["有内容", "   ", "也有内容"])
        self.assertEqual(parsed.page_count, 3)
        self.assertEqual(len(parsed.pages[0].paragraphs), 1)
        self.assertEqual(len(parsed.pages[1].paragraphs), 0)   # 空白页无段落
        self.assertEqual(len(parsed.pages[2].paragraphs), 1)

    def test_no_figures_or_captions(self):
        # MineU 路径不入库整页图:页上不挂 figure
        parsed = _pages_from_texts(["文字", "图 1 示意图"])
        for page in parsed.pages:
            self.assertEqual(page.figures, [])

    def test_empty_input(self):
        parsed = _pages_from_texts([])
        self.assertEqual(parsed.page_count, 0)
        self.assertEqual(parsed.pages, [])


if __name__ == "__main__":
    unittest.main()
