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


if __name__ == "__main__":
    unittest.main()
