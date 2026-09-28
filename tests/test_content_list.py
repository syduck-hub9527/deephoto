"""content_list.json 结构化解析:图/表/图表/标题/噪声,及 ZIP 图片读取、多块页码平移。"""

import io
import json
import unittest
import zipfile
from unittest import mock

import _bootstrap  # noqa: F401

from deephoto.parsing import content_list as cl
from deephoto.parsing.base import KIND_PARSER_IMAGE
from deephoto.parsing.captions import display_label, find_figure_mentions, find_referenced_figures, mentions_figure
from deephoto.parsing.mineru import MinerUClient, MinerUResult, _zip_extract, _zip_to_page_texts

PNG = b"\x89PNG\r\n\x1a\nfake"


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


class HtmlToTextTest(unittest.TestCase):
    def test_table_rows_become_lines(self):
        html = "<table><tr><th>类型</th><th>硅</th></tr><tr><td>N型</td><td>砷、磷、锑</td></tr></table>"
        self.assertEqual(cl.html_to_text(html), "类型 | 硅\nN型 | 砷、磷、锑")

    def test_colspan_br_and_entities(self):
        html = "<table><tr><td colspan=2>A<br>B</td></tr><tr><td>1&amp;2</td><td>x</td></tr></table>"
        self.assertEqual(cl.html_to_text(html), "A B\n1&2 | x")

    def test_missing_closing_tags_tolerated(self):
        self.assertEqual(cl.html_to_text("<table><tr><td>a<td>b"), "a | b")

    def test_plain_text_passthrough(self):
        self.assertEqual(cl.html_to_text("  256MB  0.35μm \n"), "256MB  0.35μm")
        self.assertEqual(cl.html_to_text(""), "")


class ParseContentListTest(unittest.TestCase):
    ITEMS = [
        {"type": "header", "text": "页眉", "page_idx": 0},
        {"type": "text", "text": "第1章 微电子制造引论", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "正文一。", "page_idx": 0},
        {"type": "page_number", "text": "6", "page_idx": 0},
        {"type": "image", "img_path": "images/page_0_image_1.png", "image_caption": ["图1.1 容量与尺寸"],
         "image_footnote": [], "page_idx": 0, "bbox": [100, 200, 500, 600]},
        {"type": "table", "img_path": "images/t.png", "table_caption": ["表1.1 掺杂剂"],
         "table_body": "<table><tr><td>N型</td><td>砷</td></tr></table>", "page_idx": 1},
        {"type": "chart", "img_path": "images/c.png", "chart_caption": ["图1.2 趋势"], "content": "年份 | 值\n1990 | 5",
         "page_idx": 1},
        {"type": "list", "list_items": ["a", "b"], "page_idx": 1},
        {"type": "equation", "text": "E=mc^2", "img_path": "images/e.png", "page_idx": 1},
        {"type": "mystery", "text": "未知类型应被忽略", "page_idx": 1},
        "not a dict",
        {"type": "text", "text": "无页码", "page_idx": None},
    ]
    IMAGES = {"images/page_0_image_1.png": PNG, "images/t.png": PNG + b"t", "images/c.png": PNG + b"c"}

    def setUp(self):
        self.els = cl.parse_content_list(self.ITEMS, self.IMAGES)

    def test_kinds_and_noise(self):
        kinds = [e.kind for e in self.els]
        self.assertEqual(kinds, ["heading", "text", "image", "table", "chart", "list", "equation"])
        self.assertNotIn("页眉", [e.text for e in self.els])

    def test_captions_and_images_resolved(self):
        image, table, chart = self.els[2], self.els[3], self.els[4]
        self.assertEqual(image.caption, "图1.1 容量与尺寸")
        self.assertEqual(image.image_bytes, PNG)
        self.assertEqual(image.bbox, (100.0, 200.0, 500.0, 600.0))
        self.assertEqual(table.text, "N型 | 砷")
        self.assertEqual(table.image_bytes, PNG + b"t")
        self.assertEqual(chart.text, "年份 | 值\n1990 | 5")

    def test_find_image_variants(self):
        images = {"full/images/a.png": b"A", "images/b.png": b"B"}
        self.assertEqual(cl.find_image(images, "images/a.png"), b"A")     # 后缀路径
        self.assertEqual(cl.find_image(images, "./images/b.png"), b"B")
        self.assertEqual(cl.find_image(images, "x/b.png"), b"B")          # 仅文件名
        self.assertIsNone(cl.find_image(images, "images/none.png"))
        self.assertIsNone(cl.find_image({}, "images/a.png"))
        self.assertIsNone(cl.find_image(images, None))


class BuildDocumentTest(unittest.TestCase):
    def _doc(self, items, images=None, pages=2):
        return cl.build_document(cl.parse_content_list(items, images or {}), pages)

    def test_headings_set_section_and_depth_limit(self):
        doc = self._doc([
            {"type": "text", "text": "第1章", "text_level": 1, "page_idx": 0},
            {"type": "text", "text": "1.1 掺杂", "text_level": 2, "page_idx": 0},
            {"type": "text", "text": "正文", "page_idx": 0},
            {"type": "text", "text": "深层小标题", "text_level": 5, "page_idx": 0},   # 超过 SECTION_MAX_LEVEL
            {"type": "text", "text": "仍在1.1", "page_idx": 1},
            {"type": "text", "text": "1.2 光刻", "text_level": 2, "page_idx": 1},
            {"type": "text", "text": "光刻正文", "page_idx": 1},
        ])
        secs = [(p.text, p.section) for p in doc.all_paragraphs()]
        self.assertIn(("正文", "第1章 > 1.1 掺杂"), secs)
        self.assertIn(("深层小标题", "第1章 > 1.1 掺杂"), secs)
        self.assertIn(("仍在1.1", "第1章 > 1.1 掺杂"), secs)
        self.assertIn(("光刻正文", "第1章 > 1.2 光刻"), secs)      # 同级新标题替换旧标题

    def test_image_becomes_figure_with_caption_in_text(self):
        doc = self._doc(
            [{"type": "image", "img_path": "images/a.png", "image_caption": ["图1.1 容量与尺寸"],
              "content": "256MB 0.35μm", "page_idx": 0, "bbox": [0, 0, 1000, 500]}],
            {"images/a.png": PNG})
        page = doc.pages[0]
        self.assertEqual(len(page.figures), 1)
        fig, cap = page.figures[0], page.captions[0]
        self.assertEqual((fig.kind, fig.image_bytes, fig.caption_id), (KIND_PARSER_IMAGE, PNG, cap.id))
        self.assertEqual(cap.figure_number, "1.1")
        text = page.paragraphs[0].text
        self.assertIn("图1.1 容量与尺寸", text)      # 图注必须进段落,图文关联才能判 caption_of
        self.assertIn("256MB 0.35μm", text)
        self.assertAlmostEqual(fig.bbox[3], 792.0 / 2)

    def test_table_number_namespace_and_text(self):
        doc = self._doc(
            [{"type": "table", "img_path": "images/t.png", "table_caption": ["表1.1 掺杂剂"],
              "table_body": "<table><tr><td>N型</td><td>砷、磷、锑</td></tr></table>", "page_idx": 0}],
            {"images/t.png": PNG})
        self.assertEqual(doc.pages[0].captions[0].figure_number, "表1.1")
        self.assertEqual(doc.pages[0].paragraphs[0].text, "表1.1 掺杂剂\nN型 | 砷、磷、锑")

    def test_missing_image_bytes_keeps_text_but_no_figure(self):
        doc = self._doc([{"type": "image", "img_path": "images/gone.png", "image_caption": ["图2 x"],
                          "page_idx": 0}], {})
        self.assertEqual(doc.pages[0].figures, [])
        self.assertIn("图2 x", doc.pages[0].paragraphs[0].text)

    def test_image_without_caption(self):
        doc = self._doc([{"type": "image", "img_path": "i.png", "page_idx": 0}], {"i.png": PNG})
        self.assertEqual(len(doc.pages[0].figures), 1)
        self.assertIsNone(doc.pages[0].figures[0].caption_id)
        self.assertEqual(doc.pages[0].paragraphs, [])

    def test_long_table_split_repeats_caption_and_header(self):
        rows = "".join(f"<tr><td>行{i}</td><td>{'数据' * 40}</td></tr>" for i in range(20))
        html = f"<table><tr><td>名称</td><td>值</td></tr>{rows}</table>"
        doc = self._doc([{"type": "table", "table_caption": ["表3 大表"], "table_body": html, "page_idx": 0}])
        paras = [p.text for p in doc.pages[0].paragraphs]
        self.assertGreater(len(paras), 1)
        for text in paras:
            self.assertTrue(text.startswith("表3 大表\n名称 | 值"), text[:30])
            self.assertLessEqual(len(text), cl.TABLE_PARA_CHARS + 60)

    def test_out_of_range_page_ignored(self):
        doc = self._doc([{"type": "text", "text": "越界", "page_idx": 9}], pages=2)
        self.assertEqual(doc.all_paragraphs(), [])


class CaptionNamespaceTest(unittest.TestCase):
    def test_figure_and_table_do_not_collide(self):
        self.assertEqual(find_referenced_figures("如图1.1所示,见表1.1"), ["1.1", "表1.1"])
        self.assertTrue(mentions_figure("见表 1.1", "表1.1"))
        self.assertFalse(mentions_figure("见图 1.1", "表1.1"))
        self.assertFalse(mentions_figure("见表 1.1", "1.1"))
        self.assertTrue(mentions_figure("as in Table 2", "表2"))

    def test_mentions_without_lead_word(self):
        self.assertEqual(find_figure_mentions("图 1.1 中 256MB 对应的最小特征尺寸是多少?"), ["1.1"])
        self.assertEqual(find_figure_mentions("表1.1 和 图 2 有何不同"), ["表1.1", "2"])
        self.assertEqual(find_figure_mentions("图 1.10"), ["1.10"])

    def test_display_label(self):
        self.assertEqual(display_label("1.1"), "图 1.1")
        self.assertEqual(display_label("表1.1"), "表 1.1")
        self.assertEqual(display_label(None), "图片")


class ZipExtractTest(unittest.TestCase):
    def test_images_and_elements_from_zip(self):
        items = [
            {"type": "text", "text": "正文", "page_idx": 0},
            {"type": "image", "img_path": "images/a.png", "image_caption": ["图1 x"], "page_idx": 0},
        ]
        zb = _zip({"abc/full_content_list.json".replace("full_", ""): json.dumps(items),
                   "abc/images/a.png": PNG})
        pages, elements = _zip_extract(zb, expected_pages=1)
        self.assertEqual(pages, ["正文"])
        self.assertEqual([e.kind for e in elements], ["text", "image"])
        self.assertEqual(elements[1].image_bytes, PNG)

    def test_full_md_fallback_has_no_elements(self):
        pages, elements = _zip_extract(_zip({"full.md": "整篇"}), expected_pages=1)
        self.assertEqual((pages, elements), (["整篇"], []))

    def test_legacy_wrapper_unchanged(self):
        zb = _zip({"x_content_list.json": json.dumps([{"type": "text", "text": "p0", "page_idx": 0}])})
        self.assertEqual(_zip_to_page_texts(zb), ["p0"])


class MultiChunkOffsetTest(unittest.TestCase):
    def test_page_idx_is_offset_per_chunk(self):
        def result(text):
            el = cl.ContentElement(kind="text", text=text, page_idx=1)   # 每块内 page_idx 都从 0 起
            return MinerUResult(page_texts=["", text, ""], raw={}, elements=[el])

        client = MinerUClient(api_key="tok")
        with mock.patch("deephoto.parsing.mineru.pdf_backend.chunk_pdf", return_value=[b"a", b"b"]), \
                mock.patch.object(client, "_parse_chunk", side_effect=[result("甲"), result("乙")]):
            merged = client.parse_pdf(b"%PDF", "d.pdf")
        self.assertEqual([e.page_idx for e in merged.elements], [1, 4])
        self.assertEqual(len(merged.page_texts), 6)


if __name__ == "__main__":
    unittest.main()
