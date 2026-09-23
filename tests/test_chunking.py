import unittest

import _bootstrap  # noqa: F401
from deephoto.parsing.base import ParsedParagraph
from deephoto.pipeline.chunking import MAX_CHARS, chunk_paragraphs


def para(pid, text, page=1, section=None):
    return ParsedParagraph(id=pid, text=text, page_number=page,
                           bbox=(0, 0, 100, 20), section=section)


class ChunkingTest(unittest.TestCase):
    def test_merges_small_paragraphs(self):
        chunks = chunk_paragraphs([para("p1", "第一段。" * 30), para("p2", "第二段。" * 30)])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].paragraph_ids, ["p1", "p2"])

    def test_splits_oversize(self):
        big = "字" * 800
        chunks = chunk_paragraphs([para("p1", big), para("p2", big), para("p3", big)])
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), MAX_CHARS)

    def test_section_boundary(self):
        chunks = chunk_paragraphs([
            para("p1", "内容甲。" * 60, section="第一章"),
            para("p2", "内容乙。" * 60, section="第二章"),
        ])
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].section, "第一章")
        self.assertEqual(chunks[1].section, "第二章")

    def test_page_range(self):
        chunks = chunk_paragraphs([
            para("p1", "第一页。" * 40, page=3),
            para("p2", "第二页。" * 40, page=4),
        ])
        self.assertEqual(chunks[0].page_start, 3)
        self.assertEqual(chunks[0].page_end, 4)

    def test_referenced_figures_collected(self):
        chunks = chunk_paragraphs([para("p1", "如图 3 所示,结构见图 2.1。" * 5)])
        self.assertIn("3", chunks[0].referenced_figures)
        self.assertIn("2.1", chunks[0].referenced_figures)

    def test_empty(self):
        self.assertEqual(chunk_paragraphs([]), [])


if __name__ == "__main__":
    unittest.main()
