"""虚拟分页与本地装配:分段边界、标题规则、图片归属(§3.2 / §8 语义)。"""

import unittest

import _bootstrap  # noqa: F401

from deephoto.parsing.paginate import (
    SEGMENT_HEADING_MIN_CHARS,
    SEGMENT_MAX_CHARS,
    LocalBlock,
    build_local_document,
    paginate,
)


class PaginateTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(paginate([]), [])

    def test_small_doc_single_segment(self):
        blocks = [LocalBlock("heading", "标题", level=1), LocalBlock("text", "正文")]
        self.assertEqual(paginate(blocks), [1, 1])

    def test_heading_splits_only_when_current_segment_big_enough(self):
        blocks = [
            LocalBlock("text", "x" * SEGMENT_HEADING_MIN_CHARS),
            LocalBlock("heading", "第二章", level=2),
            LocalBlock("text", "正文"),
            LocalBlock("heading", "第三章", level=2),   # 当前段不足 800:不另起
        ]
        self.assertEqual(paginate(blocks), [1, 2, 2, 2])

    def test_deep_heading_never_splits(self):
        blocks = [LocalBlock("text", "x" * 5000),
                  LocalBlock("heading", "小节", level=3)]
        self.assertEqual(paginate(blocks), [1, 2])     # 超过 3000 才切,与标题层级无关

    def test_size_limit_splits(self):
        blocks = [LocalBlock("text", "x" * (SEGMENT_MAX_CHARS + 1)), LocalBlock("text", "下一段")]
        self.assertEqual(paginate(blocks), [1, 2])

    def test_image_belongs_to_its_segment(self):
        # 前一段累计超 3000 后,图片落到新段(图片归属其出现位置所在段)
        blocks = [LocalBlock("text", "x" * (SEGMENT_MAX_CHARS + 1)),
                  LocalBlock("image", "alt", image_bytes=b"x")]
        self.assertEqual(paginate(blocks), [1, 2])

    def test_image_follows_position(self):
        blocks = [LocalBlock("text", "前文"), LocalBlock("image", "alt", image_bytes=b"x"),
                  LocalBlock("text", "后文")]
        self.assertEqual(paginate(blocks), [1, 1, 1])


class SplitOversizeTest(unittest.TestCase):
    def test_oversize_text_block_splits_at_line_boundaries(self):
        # 回归:超过约 1500 字的块先按行拆开,再分页(否则整份 txt 落一个分段)
        text = "\n".join("行" * 100 for _ in range(60))         # 约 6KB
        doc = build_local_document([LocalBlock("text", text)])
        self.assertGreater(doc.page_count, 1)
        for p in doc.all_paragraphs():
            self.assertLessEqual(len(p.text), 1600)

    def test_single_overlong_line_hard_cut(self):
        doc = build_local_document([LocalBlock("text", "x" * 5000)])
        self.assertGreaterEqual(len(doc.all_paragraphs()), 4)
        for p in doc.all_paragraphs():
            self.assertLessEqual(len(p.text), 1500)

    def test_headings_and_images_not_split(self):
        long_heading = "标" * 2000
        blocks = [LocalBlock("heading", long_heading, level=1),
                  LocalBlock("image", "alt", image_bytes=b"\x89PNG" + b"0" * 2000)]
        doc = build_local_document(blocks)
        self.assertEqual(doc.all_paragraphs()[0].text, long_heading)   # 标题不拆
        self.assertEqual(len(doc.all_figures()), 1)                    # 图不拆


class BuildLocalDocumentTest(unittest.TestCase):
    def test_sections_and_caption_linkage(self):
        blocks = [
            LocalBlock("heading", "第1章 引论", level=1),
            LocalBlock("text", "正文一段"),
            LocalBlock("image", "图 1.1 容量进展", image_bytes=b"\x89PNG", caption="图 1.1 容量进展"),
            LocalBlock("heading", "1.1 小节", level=2),
            LocalBlock("text", "小节正文"),
        ]
        doc = build_local_document(blocks)
        self.assertEqual(doc.locator_kind, "section")
        self.assertEqual(doc.page_count, 1)
        paras = doc.all_paragraphs()
        # 标题自身也落段落,section 路径含祖先
        self.assertEqual(paras[0].section, "第1章 引论")
        self.assertEqual(paras[-1].section, "第1章 引论 > 1.1 小节")
        # 图注落段落(caption_of 关联依赖块内含图注文本)
        self.assertIn("图 1.1 容量进展", [p.text for p in paras])
        fig = doc.all_figures()[0]
        cap = doc.all_captions()[0]
        self.assertEqual(fig.caption_id, cap.id)
        self.assertEqual(cap.figure_number, "1.1")
        self.assertEqual(fig.kind, "embedded_bitmap")

    def test_image_without_bytes_becomes_text_only(self):
        blocks = [LocalBlock("image", "远程图 alt", image_bytes=None)]
        doc = build_local_document(blocks)
        self.assertEqual(doc.all_figures(), [])
        self.assertEqual([p.text for p in doc.all_paragraphs()], ["远程图 alt"])

    def test_empty_blocks_yield_zero_pages(self):
        doc = build_local_document([])
        self.assertEqual(doc.page_count, 0)            # validate_parsed 会拒绝,见 registry 测试


if __name__ == "__main__":
    unittest.main()
