"""Markdown/txt 本地解析器测试(§3.4d/§3.4e,§8 语义与安全)。"""

import base64
import unittest
from unittest import mock

import _bootstrap  # noqa: F401

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

from deephoto.config import Settings
from deephoto.parsing.formats import format_by_key
from deephoto.parsing.markdown_parser import MarkdownParser, _strip_front_matter
from deephoto.parsing.registry import SourceFile, validate_parsed
from deephoto.parsing.text_parser import TextParser


def _settings(**overrides):
    kwargs = dict(
        moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
        embedding_base_url=None, embedding_api_key=None, embedding_model=None,
        data_dir="/tmp/x", max_upload_mb=100, ingestion_version="v2", mineru_api_key=None,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class _Recorder:
    """记录告警的最小观察器。"""

    def __init__(self):
        self.warnings: list[str] = []

    def warn(self, message):
        self.warnings.append(message)


def _png_b64() -> str:
    import io
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _md(data: str, observer=None, **settings_over):
    parser = MarkdownParser(_settings(**settings_over))
    src = SourceFile(data=data.encode("utf-8"), filename="a.md", fmt=format_by_key("md"))
    return parser.parse(src, observer or _Recorder())


class FrontMatterTest(unittest.TestCase):
    def test_front_matter_stripped(self):
        self.assertEqual(_strip_front_matter("---\ntitle: 测试\ndate: 2026\n---\n# 正文\n"), "# 正文\n")
        self.assertEqual(_strip_front_matter("# 无 front matter"), "# 无 front matter")

    def test_hr_then_text_then_hr_is_not_front_matter(self):
        # 回归:以分隔线开头、后文还有分隔线,正文不得被当成 YAML 吞掉
        src = "---\n\n重要前言\n\n---\n\n# 标题\n正文"
        self.assertEqual(_strip_front_matter(src), src)
        doc = _md(src)
        self.assertIn("重要前言", [p.text for p in doc.all_paragraphs()])

    def test_closing_fence_beyond_100_lines_not_stripped(self):
        body = "\n".join(f"key{i}: v" for i in range(101))
        src = f"---\n{body}\n---\n正文"
        self.assertEqual(_strip_front_matter(src), src)   # 闭合行超过前 100 行:不剥离

    def test_front_matter_not_in_section(self):
        doc = _md("---\ntitle: 不该进章节\n---\n# 第一章 引论\n正文\n")
        sections = [p.section for p in doc.all_paragraphs()]
        self.assertTrue(all(s is None or "不该进章节" not in s for s in sections))
        self.assertEqual(doc.all_paragraphs()[0].text, "第一章 引论")
        self.assertEqual(doc.all_paragraphs()[0].section, "第一章 引论")


@unittest.skipUnless(HAVE_PIL, "需要 pillow")
class MarkdownParseTest(unittest.TestCase):
    def test_structure_table_fence_list_kept_raw(self):
        src = ("# 标题\n\n正文**粗体**。\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
               "```python\nprint(1)\n```\n\n- 甲\n- 乙\n")
        doc = _md(src)
        texts = [p.text for p in doc.all_paragraphs()]
        self.assertTrue(any(t.startswith("| a | b |") for t in texts))   # 表格原文保留
        self.assertIn("```python\nprint(1)\n```", texts)                  # 围栏原文保留
        self.assertTrue(any("- 甲" in t for t in texts))                  # 列表原文保留
        validate_parsed(doc)

    def test_data_uri_image_with_caption(self):
        src = f"看图:\n\n![图 2.1 容量曲线](data:image/png;base64,{_png_b64()})\n\n图 2.1 容量曲线\n"
        doc = _md(src)
        figs = doc.all_figures()
        self.assertEqual(len(figs), 1)
        cap = doc.all_captions()[0]
        self.assertEqual(figs[0].caption_id, cap.id)
        self.assertEqual(cap.figure_number, "2.1")
        # 图注文本落在段落里(caption_of 关联的前提)
        self.assertIn("图 2.1 容量曲线", [p.text for p in doc.all_paragraphs()])
        validate_parsed(doc)

    def test_remote_image_never_fetched(self):
        observer = _Recorder()
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("不得发起网络请求")) as mocked:
            doc = _md("![流程图](https://example.com/a.png)\n", observer)
        mocked.assert_not_called()
        self.assertEqual(doc.all_figures(), [])
        self.assertEqual([p.text for p in doc.all_paragraphs()], ["流程图"])   # alt 入段落
        self.assertEqual(observer.warnings, [])                                # 远程图不告警

    def test_relative_image_warns_and_keeps_alt(self):
        observer = _Recorder()
        doc = _md("![示意图](images/a.png)\n", observer)
        self.assertEqual(doc.all_figures(), [])
        self.assertEqual([p.text for p in doc.all_paragraphs()], ["示意图"])
        self.assertEqual(len(observer.warnings), 1)
        self.assertIn("未随文档上传", observer.warnings[0])

    def test_oversize_or_undecodable_data_uri_skipped(self):
        observer = _Recorder()
        doc = _md("![x](data:image/png;base64,%%%invalid%%%)\n", observer)
        self.assertEqual(doc.all_figures(), [])
        self.assertEqual(len(observer.warnings), 1)
        # 超限:限制调到 1MB,构造 1MB+ 的合法 base64 填充(解码后 Pillow 打不开也行,先过尺寸关)
        observer2 = _Recorder()
        big = base64.b64encode(b"\x89PNG" + b"0" * (1024 * 1024 + 1)).decode()
        doc2 = _md(f"![x](data:image/png;base64,{big})\n", observer2,
                   markdown_data_uri_max_mb=1)
        self.assertEqual(doc2.all_figures(), [])
        self.assertTrue(any("超过内联上限" in w for w in observer2.warnings))

    def test_html_block_stripped_and_script_dropped(self):
        doc = _md("前文\n\n<div><p>说明文字</p><script>alert(1)</script></div>\n")
        texts = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("说明文字", texts)
        self.assertNotIn("alert", texts)
        self.assertNotIn("<div", texts)

    def test_gbk_encoded_md(self):
        parser = MarkdownParser(_settings())
        src = SourceFile(data="# 标题\n\n正文".encode("gb18030"), filename="a.md",
                         fmt=format_by_key("md"))
        doc = parser.parse(src, _Recorder())
        self.assertEqual(doc.all_paragraphs()[0].text, "标题")

    def test_sections_from_heading_levels(self):
        doc = _md("# 章\n\n## 节\n\n内容\n\n# 第二章\n\n内容2\n")
        sections = [p.section for p in doc.all_paragraphs() if p.text.startswith("内容")]
        self.assertEqual(sections, ["章 > 节", "第二章"])

    def test_images_inside_list_and_table_keep_only_alt(self):
        # 回归:容器块整块切原文,![alt](data:...) 的 base64 长串不得进正文
        big_b64 = "A" * 2000
        src = (f"- 条目 ![图 9 曲线](data:image/png;base64,{big_b64}) 尾巴\n"
               f"| 列 |\n| --- |\n| ![单元图](data:image/png;base64,{big_b64}) |\n")
        doc = _md(src)
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("条目", blob)
        self.assertIn("图 9 曲线", blob)          # alt 保留
        self.assertIn("单元图", blob)
        self.assertNotIn("base64", blob)           # 长串抹掉
        self.assertNotIn("![", blob)

    def test_hardbreak_kept(self):
        # 回归:行尾两空格的硬换行被丢弃,文字粘在一起
        doc = _md("第一行  \nsecond line  \nthird\n")
        self.assertEqual(doc.all_paragraphs()[0].text, "第一行\nsecond line\nthird")

    def test_giant_fence_split_across_segments(self):
        # 回归:超大围栏整块落一个分段;现按行拆开再分页
        fence_body = "\n".join(f"line {i} " + "x" * 60 for i in range(120))   # 约 8KB
        doc = _md(f"```\n{fence_body}\n```\n")
        self.assertGreater(doc.page_count, 1)
        for p in doc.all_paragraphs():
            self.assertLessEqual(len(p.text), 1600)

    def test_utf16_bom_md(self):
        # 回归:Windows 记事本"Unicode"(UTF-16 带 BOM)不得按二进制拒绝
        data = "# 标题\n\n正文 utf16".encode("utf-16")   # 带 BOM,含大量 NUL
        parser = MarkdownParser(_settings())
        doc = parser.parse(SourceFile(data=data, filename="a.md", fmt=format_by_key("md")),
                           _Recorder())
        self.assertEqual(doc.all_paragraphs()[0].text, "标题")

    def test_fence_keeps_image_syntax_raw(self):
        # 容器块抹图片语法,但围栏代码是示例文本,原样保留(含围栏标记)
        doc = _md("```\n![a](data:image/png;base64,AAAA)\n```\n")
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("![a](data:image/png;base64,AAAA)", blob)

    def test_inline_img_tag_data_uri_not_in_body(self):
        # 回归:行内 <img src="data:..."> 的 base64 长串进正文(与 ![](...) 同类)
        big = "A" * 2000
        doc = _md(f'正文 <img src="data:image/png;base64,{big}"> 后文\n')
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("正文", blob)
        self.assertIn("后文", blob)
        self.assertNotIn("base64", blob)
        self.assertNotIn("<img", blob)

    def test_inline_img_tag_keeps_alt(self):
        doc = _md(f'看 <img alt="流程示意" src="data:image/png;base64,AAAA"> 这里\n')
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("流程示意", blob)
        self.assertNotIn("base64", blob)

    def test_img_tag_inside_list_stripped(self):
        big = "A" * 2000
        doc = _md(f'- 条目 <img src="data:image/png;base64,{big}"> 尾巴\n')
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("条目", blob)
        self.assertIn("尾巴", blob)
        self.assertNotIn("base64", blob)
        self.assertNotIn("<img", blob)

    def test_fence_keeps_img_tag_raw(self):
        doc = _md('```\n<img src="data:image/png;base64,AAAA">\n```\n')
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn('<img src="data:image/png;base64,AAAA">', blob)

    def test_angle_brackets_that_are_not_tags_survive(self):
        # 回归:容器原文一刀切 <[^>]+>,泛型/比较符被当标签误删
        doc = _md("- std::vector<int> 与 a<b 且 c>d\n\n| 列 |\n| --- |\n| Map<K,V> |\n")
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("std::vector<int>", blob)
        self.assertIn("a<b 且 c>d", blob)
        self.assertIn("Map<K,V>", blob)

    def test_top_level_generics_survive(self):
        # 顶层段落同规则(markdown-it 会把 <int> 当 html_inline,白名单不放行)
        doc = _md("正文 std::vector<int> 后文\n")
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("std::vector<int>", blob)

    def test_autolink_kept(self):
        doc = _md("- 见 <https://example.com/a> 即可\n")
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("<https://example.com/a>", blob)

    def test_inline_code_in_container_kept_raw(self):
        # 容器内的行内代码是示例文本:其中的标签/图片语法都不动
        doc = _md("- 示例 `<b>x</b>` 与 ![图](data:image/png;base64,AAAA)\n")
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertIn("`<b>x</b>`", blob)      # 行内代码原样
        self.assertNotIn("base64", blob)       # 代码外的图片语法仍只留 alt
        self.assertIn("图", blob)


class TextParserTest(unittest.TestCase):
    def test_blank_line_segments(self):
        parser = TextParser(_settings())
        src = SourceFile(data="第一段\n换行不断段\n\n第二段\n".encode(), filename="a.txt",
                         fmt=format_by_key("txt"))
        doc = parser.parse(src, _Recorder())
        texts = [p.text for p in doc.all_paragraphs()]
        self.assertEqual(texts, ["第一段\n换行不断段", "第二段"])
        self.assertEqual(doc.locator_kind, "section")
        self.assertTrue(all(p.section is None for p in doc.all_paragraphs()))
        validate_parsed(doc)

    def test_crlf_normalized(self):
        parser = TextParser(_settings())
        src = SourceFile(data="一\r\n\r\n二\r\n".encode(), filename="a.txt", fmt=format_by_key("txt"))
        doc = parser.parse(src, _Recorder())
        self.assertEqual([p.text for p in doc.all_paragraphs()], ["一", "二"])

    def test_single_newline_only_falls_back_to_lines(self):
        # 回归:800 行单换行的 txt 修前只有 1 页 1 个超长段落
        text = "\n".join(f"第 {i} 行 " + "字" * 25 for i in range(800))   # 约 2.4 万字,无空行
        parser = TextParser(_settings())
        doc = parser.parse(SourceFile(data=text.encode(), filename="a.txt",
                                      fmt=format_by_key("txt")), _Recorder())
        self.assertGreater(doc.page_count, 1)                  # 虚拟分页生效
        self.assertGreater(len(doc.all_paragraphs()), 100)     # 按行成段
        for p in doc.all_paragraphs():
            self.assertLessEqual(len(p.text), 1600)


if __name__ == "__main__":
    unittest.main()
