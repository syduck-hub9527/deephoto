import unittest

import _bootstrap  # noqa: F401
from deephoto.parsing.base import ParsedParagraph
from deephoto.pipeline.chunking import MAX_CHARS, chunk_paragraphs, split_long_text


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

    def test_single_oversize_paragraph_is_split(self):
        # MinerU 整页文本 = 一个段落,过去会原样成为一个 2000 字的块
        text = "这是一个完整的句子,用来测试拆分。" * 150      # 约 2400 字
        chunks = chunk_paragraphs([para("p1_mineru", text)])
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), MAX_CHARS)
        self.assertEqual("".join(c.text.replace("\n", "") for c in chunks), text)   # 不丢字
        self.assertEqual(chunks[0].paragraph_ids[0], "p1_mineru#1")

    def test_small_head_plus_big_paragraph_respects_cap(self):
        # 过去:100 字 + 1500 字 会被追加成 1601 字的块
        chunks = chunk_paragraphs([para("p1", "甲" * 100), para("p2", "乙。" * 750)])
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), MAX_CHARS)

    def test_split_prefers_sentence_boundary(self):
        pieces = split_long_text("第一句。" * 400, limit=900)
        for piece in pieces:
            self.assertTrue(piece.endswith("。"))
            self.assertLessEqual(len(piece), 900)

    def test_split_keeps_space_after_english_period(self):
        # 回归:曾因正则消耗了句号后的空白,得到 "wafer.Photolithography" 这样的粘连词,
        # 且 BM25 分词会把它当成一个词,导致英文检索失效
        text = "Photolithography transfers the mask pattern onto the wafer. " * 30
        pieces = split_long_text(text, limit=900)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(" ".join(pieces).split(), text.split())      # 不丢词、词间空白保留
        for piece in pieces:
            self.assertNotIn("wafer.Photolithography", piece)

    def test_split_hard_cuts_punctuation_free_text(self):
        pieces = split_long_text("字" * 2500, limit=900)
        self.assertEqual([len(x) for x in pieces], [900, 900, 700])

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
