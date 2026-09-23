import unittest

import _bootstrap  # noqa: F401
from deephoto.parsing.captions import (
    asks_for_images,
    find_referenced_figures,
    match_caption,
    mentions_figure,
)


class CaptionMatchTest(unittest.TestCase):
    def test_chinese_figure(self):
        num, text = match_caption("图 3:注意力机制结构图")
        self.assertEqual(num, "3")
        self.assertIn("注意力机制结构图", text)

    def test_chinese_no_space(self):
        num, _ = match_caption("图3 训练流程")
        self.assertEqual(num, "3")

    def test_dotted_number(self):
        num, _ = match_caption("图 2.1 整体架构")
        self.assertEqual(num, "2.1")

    def test_english_fig(self):
        num, _ = match_caption("Fig. 4a Architecture of the model")
        self.assertEqual(num, "4a")

    def test_table(self):
        num, _ = match_caption("Table 2 - Results")
        self.assertEqual(num, "2")

    def test_not_caption(self):
        num, text = match_caption("本文提出一种新方法。")
        self.assertIsNone(num)

    def test_mid_sentence_number_not_caption(self):
        num, _ = match_caption("结果见图 3 所示")
        self.assertIsNone(num)   # 引用不是图注


class ReferenceTest(unittest.TestCase):
    def test_find_references(self):
        nums = find_referenced_figures("如图 3 所示,流程见图 2.1;另有 as shown in Fig. 4 的细节。")
        self.assertEqual(nums, ["3", "2.1", "4"])

    def test_dedup_and_order(self):
        nums = find_referenced_figures("见图 3,再见图 3")
        self.assertEqual(nums, ["3"])

    def test_no_reference(self):
        self.assertEqual(find_referenced_figures("没有任何引用。"), [])

    def test_mentions_figure(self):
        self.assertTrue(mentions_figure("如图 3 所示", "3"))
        self.assertFalse(mentions_figure("如图 30 所示", "3"))   # 不截断匹配
        self.assertTrue(mentions_figure("see Fig. 4a", "4a"))

    def test_asks_for_images(self):
        self.assertTrue(asks_for_images("把相关图也给我看"))
        self.assertTrue(asks_for_images("show me the diagram"))
        self.assertFalse(asks_for_images("什么是注意力机制"))


if __name__ == "__main__":
    unittest.main()
