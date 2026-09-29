"""pptx 本地解析器测试:fixture 全部由 python-pptx 程序生成,不提交二进制样本(§8)。"""

import io
import unittest
import zipfile

import _bootstrap  # noqa: F401

try:
    import numpy  # noqa: F401
    from PIL import Image
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

from deephoto.config import Settings
from deephoto.parsing.formats import format_by_key
from deephoto.parsing.registry import (
    SourceFile,
    create_parser,
    engine_for,
    validate_parsed,
)


def _settings(**overrides):
    kwargs = dict(
        moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
        embedding_base_url=None, embedding_api_key=None, embedding_model=None,
        data_dir="/tmp/x", max_upload_mb=100, ingestion_version="v2", mineru_api_key=None,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class _Recorder:
    def __init__(self):
        self.warnings: list[str] = []

    def warn(self, message):
        self.warnings.append(message)


def _png(color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _pptx(build) -> bytes:
    from pptx import Presentation
    prs = Presentation()
    build(prs)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _blank(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])   # 空白版式


def _textbox(slide, text, top_in, left_in):
    from pptx.util import Inches
    box = slide.shapes.add_textbox(Inches(left_in), Inches(top_in), Inches(4), Inches(0.5))
    box.text_frame.text = text
    return box


def _parse(data: bytes, observer=None, **settings_over):
    from deephoto.parsing.pptx_parser import PptxParser
    src = SourceFile(data=data, filename="a.pptx", fmt=format_by_key("pptx"))
    return PptxParser(_settings(**settings_over)).parse(src, observer or _Recorder())


@unittest.skipUnless(HAVE_DEPS, "需要 pillow 与 python-pptx")
class PptxParserTest(unittest.TestCase):
    def test_title_section_and_locator(self):
        def build(prs):
            s = prs.slides.add_slide(prs.slide_layouts[0])
            s.shapes.title.text = "第一章 概览"
            s.placeholders[1].text = "要点一"
            s2 = _blank(prs)
            _textbox(s2, "无标题页的正文", 1, 1)
        doc = _parse(_pptx(build))
        self.assertEqual(doc.locator_kind, "slide")
        self.assertEqual(doc.page_count, 2)
        texts = {p.text: p.section for p in doc.all_paragraphs()}
        self.assertEqual(texts["第一章 概览"], "幻灯片 1:第一章 概览")   # 标题也落段落(可检索)
        self.assertEqual(texts["要点一"], "幻灯片 1:第一章 概览")
        self.assertEqual(texts["无标题页的正文"], "幻灯片 2")            # 无标题:序号即 section
        validate_parsed(doc)

    def test_reading_order_by_position_not_zorder(self):
        # 后加的形状在层叠序靠后,但位置更高:阅读顺序应按 (top, left)
        def build(prs):
            s = _blank(prs)
            _textbox(s, "下方的段落", 3, 1)
            _textbox(s, "上方的段落", 1, 1)
            _textbox(s, "同排右侧", 1, 6)
        doc = _parse(_pptx(build))
        texts = [p.text for p in doc.pages[0].paragraphs]
        self.assertEqual(texts, ["上方的段落", "同排右侧", "下方的段落"])

    def test_notes_prefixed(self):
        def build(prs):
            s = _blank(prs)
            _textbox(s, "正文内容", 1, 1)
            s.notes_slide.notes_text_frame.text = "这句是讲者备注"
        doc = _parse(_pptx(build))
        texts = [p.text for p in doc.pages[0].paragraphs]
        self.assertEqual(texts[-1], "备注:这句是讲者备注")

    def test_image_caption_below_paired(self):
        from pptx.util import Inches

        def build(prs):
            s = _blank(prs)
            s.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(1))
            _textbox(s, "图 2.1 容量与尺寸", 2.5, 1)
        doc = _parse(_pptx(build))
        figs = doc.all_figures()
        self.assertEqual(len(figs), 1)
        caps = doc.all_captions()
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].figure_number, "2.1")
        self.assertEqual(figs[0].caption_id, caps[0].id)
        # 图注文本落在段落里恰好一份(caption_of 关联的前提)
        texts = [p.text for p in doc.all_paragraphs()]
        self.assertEqual(texts.count("图 2.1 容量与尺寸"), 1)
        validate_parsed(doc)

    def test_caption_above_image_not_paired(self):
        # 图注样式的文本框在图片上方:位置约束拒绝配对,落为普通文本
        from pptx.util import Inches

        def build(prs):
            s = _blank(prs)
            _textbox(s, "图 1.1 在上不在下", 1, 1)
            s.shapes.add_picture(io.BytesIO(_png()), Inches(2.5), Inches(1))
        doc = _parse(_pptx(build))
        figs = doc.all_figures()
        self.assertEqual(len(figs), 1)
        self.assertIsNone(figs[0].caption_id)
        self.assertEqual(doc.all_captions(), [])
        texts = [p.text for p in doc.all_paragraphs()]
        self.assertEqual(texts.count("图 1.1 在上不在下"), 1)

    def test_side_by_side_captions_not_stolen(self):
        # 并排双图各自带下方图注:阅读序是 图1 图2 注1 注2,
        # 图2 不得抢走注1(水平不重叠),注2 跳过已配对的注1 配给图2
        from pptx.util import Inches

        def build(prs):
            s = _blank(prs)
            s.shapes.add_picture(io.BytesIO(_png("red")), Inches(1), Inches(1))
            s.shapes.add_picture(io.BytesIO(_png("blue")), Inches(5), Inches(1))
            _textbox(s, "图 1.1 左图", 2.5, 1)
            _textbox(s, "图 1.2 右图", 2.5, 5)
        doc = _parse(_pptx(build))
        figs = doc.all_figures()
        self.assertEqual(len(figs), 2)
        caps = {c.id: c for c in doc.all_captions()}
        self.assertEqual(caps[figs[0].caption_id].figure_number, "1.1")
        self.assertEqual(caps[figs[1].caption_id].figure_number, "1.2")
        validate_parsed(doc)

    def test_group_shape_recursive(self):
        from pptx.util import Inches

        def build(prs):
            s = _blank(prs)
            grp = s.shapes.add_group_shape()
            box = grp.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
            box.text_frame.text = "组合里的文字"
            grp.shapes.add_picture(io.BytesIO(_png("green")), Inches(2), Inches(1))
        doc = _parse(_pptx(build))
        texts = [p.text for p in doc.all_paragraphs()]
        self.assertIn("组合里的文字", texts)
        self.assertEqual(len(doc.all_figures()), 1)   # 组内图片也取出
        validate_parsed(doc)

    def test_table_rows_and_merged_cells(self):
        from pptx.util import Inches

        def build(prs):
            s = _blank(prs)
            gfx = s.shapes.add_table(2, 3, Inches(1), Inches(1), Inches(6), Inches(1))
            t = gfx.table
            t.rows[0].cells[0].merge(t.rows[0].cells[2])
            t.rows[0].cells[0].text = "总标题合并"
            t.rows[1].cells[0].text = "甲"
            t.rows[1].cells[1].text = "乙"
            t.rows[1].cells[2].text = "丙"
        doc = _parse(_pptx(build))
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertEqual(blob.count("总标题合并"), 1)   # 被合并的格子不重复输出
        self.assertIn("甲 | 乙 | 丙", blob)

    def test_emf_image_skipped_with_warning(self):
        # zip 级手术:把内嵌 PNG 改名为 .emf 并改 ContentType,模拟 EMF 图
        from pptx.util import Inches

        def build(prs):
            s = _blank(prs)
            _textbox(s, "正文保留", 3, 1)
            s.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(1))
        data = _pptx(build)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w") as zout:
            for info in zin.infolist():
                payload = zin.read(info.filename)
                name = info.filename
                if name.startswith("ppt/media/"):
                    name = name.rsplit(".", 1)[0] + ".emf"
                if name == "[Content_Types].xml":
                    payload = payload.replace(b'Extension="png" ContentType="image/png"',
                                              b'Extension="emf" ContentType="image/x-emf"')
                if name.endswith(".rels"):
                    import re as _re
                    payload = _re.sub(rb'media/([\w.-]+)\.png', rb'media/\1.emf', payload)
                zout.writestr(name, payload)
        observer = _Recorder()
        doc = _parse(buf.getvalue(), observer)
        self.assertEqual(doc.all_figures(), [])
        self.assertTrue(any("跳过暂不支持的图片格式" in w for w in observer.warnings))
        self.assertIn("正文保留", [p.text for p in doc.all_paragraphs()])

    def test_entity_probe_not_expanded(self):
        # 外部实体探针:python-pptx 与 python-docx 共用 oxml 解析器,实体不展开
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                       '<Default Extension="xml" ContentType="application/xml"/>'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                       '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>')
            z.writestr("_rels/.rels",
                       '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>')
            z.writestr("ppt/presentation.xml",
                       '<?xml version="1.0"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                       '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>')
            z.writestr("ppt/_rels/presentation.xml.rels",
                       '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>')
            z.writestr("ppt/slides/slide1.xml",
                       '<?xml version="1.0"?><!DOCTYPE p:sld [<!ENTITY xxe "EXPANDED-SECRET">]>'
                       '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                       '<p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>'
                       '<p:sp><p:nvSpPr><p:cNvPr id="2" name="tb"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>'
                       '<p:txBody><a:bodyPr/><a:p><a:r><a:t>正文 &xxe; 结束</a:t></a:r></a:p></p:txBody></p:sp>'
                       '</p:spTree></p:cSld></p:sld>')
        doc = _parse(buf.getvalue())
        blob = "\n".join(p.text for p in doc.all_paragraphs())
        self.assertNotIn("EXPANDED-SECRET", blob)
        self.assertIn("正文", blob)

    def test_engine_selection(self):
        from deephoto.parsing.pptx_parser import PptxParser
        fmt = format_by_key("pptx")
        self.assertEqual(engine_for(fmt, _settings()), "local")
        self.assertEqual(engine_for(fmt, _settings(pptx_parser="mineru")), "mineru")
        self.assertIsInstance(create_parser(fmt, _settings()), PptxParser)


if __name__ == "__main__":
    unittest.main()
