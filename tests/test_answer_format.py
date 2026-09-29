import unittest

import _bootstrap  # noqa: F401
from deephoto.agent.answer_format import normalize_image_anchors as norm


class NormalizeImageAnchorsTest(unittest.TestCase):
    def test_midline_anchor_moves_after_its_line(self):
        # 回归:锚点写在句中,会把句尾的“(第 2 页)。”甩到图片下方
        out = norm("- 呈**指数增长**[image:occ_a](第 2 页)。\n- 下一条")
        self.assertEqual(out, "- 呈**指数增长**(第 2 页)。\n\n[image:occ_a]\n\n- 下一条")

    def test_standalone_anchor_kept(self):
        text = "先看。\n\n[image:occ_a]\n\n再解释。"
        self.assertEqual(norm(text), text)

    def test_duplicate_anchor_dropped_without_blank_hole(self):
        # 回归:同一张图第二次出现渲染成孤立徽标,且前后留下大片空白
        out = norm("甲\n\n[image:occ_a]\n\n乙\n\n[image:occ_a]\n\n丙")
        self.assertEqual(out, "甲\n\n[image:occ_a]\n\n乙\n\n丙")

    def test_duplicate_inside_sentence_only_marker_removed(self):
        out = norm("[image:occ_a]\n\n如图[image:occ_a]所示。")
        self.assertEqual(out, "[image:occ_a]\n\n如图所示。")

    def test_two_different_anchors_on_one_line(self):
        out = norm("对比[image:occ_a]和[image:occ_b]两图。")
        self.assertEqual(out, "对比和两图。\n\n[image:occ_a]\n\n[image:occ_b]")

    def test_code_spans_and_fences_untouched(self):
        text = "写法是 `[image:occ_x]`。\n```\n[image:occ_y]\n```\n完"
        self.assertEqual(norm(text), text)

    def test_blank_runs_collapsed_and_text_without_anchor_unchanged(self):
        self.assertEqual(norm("甲\n\n\n\n乙"), "甲\n\n乙")
        self.assertEqual(norm("## 标题\n- a\n- b"), "## 标题\n- a\n- b")

    def test_chunk_markers_untouched(self):
        self.assertEqual(norm("事实[chunk:chk_1]。"), "事实[chunk:chk_1]。")

    def test_idempotent(self):
        once = norm("句中[image:occ_a]继续。\n\n[image:occ_a]\n尾")
        self.assertEqual(norm(once), once)


if __name__ == "__main__":
    unittest.main()
