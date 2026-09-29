"""图片格式:本地降级解析器(§3.4f)、引擎选择矩阵、MinerU 路径补原图(§2 矩阵)。"""

import io
import unittest

import _bootstrap  # noqa: F401

try:
    from PIL import Image
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

from deephoto.config import Settings
from deephoto.parsing.formats import UnsupportedFormat, format_by_key
from deephoto.parsing.registry import (
    EngineUnavailable,
    SourceFile,
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


def _png(color="red", size=(8, 6)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@unittest.skipUnless(HAVE_DEPS, "需要 pillow")
class ImageParserTest(unittest.TestCase):
    def _parse(self, data, observer=None):
        from deephoto.parsing.image_parser import ImageParser
        src = SourceFile(data=data, filename="a.png", fmt=format_by_key("image"))
        return ImageParser(_settings()).parse(src, observer or _Recorder())

    def test_whole_image_single_figure(self):
        observer = _Recorder()
        doc = self._parse(_png(), observer)
        self.assertEqual(doc.locator_kind, "page")
        self.assertEqual(doc.page_count, 1)
        figs = doc.all_figures()
        self.assertEqual(len(figs), 1)
        self.assertTrue(figs[0].image_bytes.startswith(b"\x89PNG"))
        self.assertEqual((doc.pages[0].width, doc.pages[0].height), (8.0, 6.0))
        self.assertTrue(any("未做 OCR" in w for w in observer.warnings))
        validate_parsed(doc)

    def test_corrupt_image_readable_error(self):
        with self.assertRaisesRegex(UnsupportedFormat, "无法解码"):
            self._parse(b"\x89PNG\r\n\x1a\n" + b"garbage")


class EngineSelectionTest(unittest.TestCase):
    """引擎选择矩阵(§2/§3.4f):旧版 Office 只能 MinerU;图片 MinerU 优先,
    无 Token 且开描述模型才降级本地;都没有给可读原因。"""

    def test_image_prefers_mineru_with_token(self):
        self.assertEqual(
            engine_for(format_by_key("image"), _settings(mineru_api_key="tok")), "mineru")

    def test_image_degrades_to_local_with_description_only(self):
        settings = _settings(description_enabled=True, description_api_key="k",
                             description_base_url="http://localhost/v1")
        self.assertEqual(engine_for(format_by_key("image"), settings), "local")

    def test_image_without_token_and_description_is_readable_error(self):
        with self.assertRaisesRegex(EngineUnavailable, "DEEPHOTO_MINERU_API_KEY"):
            engine_for(format_by_key("image"), _settings())

    def test_legacy_office_requires_mineru_token(self):
        for key in ("doc", "ppt", "xls"):
            self.assertEqual(
                engine_for(format_by_key(key), _settings(mineru_api_key="tok")), "mineru")
            with self.assertRaisesRegex(EngineUnavailable, "DEEPHOTO_MINERU_API_KEY"):
                engine_for(format_by_key(key), _settings())


@unittest.skipUnless(HAVE_DEPS, "需要 pillow")
class MinerUImageFigureTest(unittest.TestCase):
    """MinerU 对图片输入只回 OCR 文本(样本实测无 image 元素);原图补为唯一 figure。"""

    def _parse_with_fake(self, elements, page_texts):
        from deephoto.parsing.mineru import MinerUResult
        from deephoto.parsing.mineru_parser import MinerUParser

        class _FakeClient:
            def parse_file(self, data, filename, fmt):
                return MinerUResult(page_texts=page_texts, raw={}, elements=elements)

        png = _png()
        src = SourceFile(data=png, filename="a.png", fmt=format_by_key("image"))
        parser = MinerUParser(_settings(mineru_api_key="tok"),
                              client_factory=lambda obs: _FakeClient())
        return parser.parse(src, _Recorder()), png

    def test_original_image_attached_alongside_ocr_text(self):
        from deephoto.parsing.content_list import parse_content_list
        items = [{"type": "text", "text": "OCR 出的文字", "page_idx": 0}]
        doc, png = self._parse_with_fake(parse_content_list(items, {}), ["OCR 出的文字"])
        self.assertIn("OCR 出的文字", [p.text for p in doc.all_paragraphs()])
        figs = doc.all_figures()
        self.assertEqual(len(figs), 1)
        self.assertEqual(figs[0].image_bytes, png)       # 原图字节直接入库
        self.assertEqual(figs[0].page_number, 1)
        validate_parsed(doc)

    def test_blank_ocr_still_ingests_with_figure(self):
        # OCR 无任何文字:整图仍在,validate_parsed 不拒绝(有图),靠描述检索
        doc, png = self._parse_with_fake([], [""])
        self.assertEqual(doc.page_count, 1)
        self.assertEqual(len(doc.all_figures()), 1)
        validate_parsed(doc)

    def test_mineru_subfigures_replaced_by_original(self):
        # MinerU 若对图片输入返回了子图裁剪(截图/图表类未实测):以原图为准,
        # 子图丢弃不并存(否则 QA 重复配图);子图的图注文本仍留在段落里可检索
        from deephoto.parsing.content_list import parse_content_list
        items = [{"type": "image", "img_path": "images/sub.png",
                  "image_caption": ["图 1 子图"], "page_idx": 0},
                 {"type": "text", "text": "OCR 文字", "page_idx": 0}]
        elements = parse_content_list(items, {"images/sub.png": _png("blue")})
        doc, png = self._parse_with_fake(elements, ["OCR 文字"])
        figs = doc.all_figures()
        self.assertEqual(len(figs), 1)                   # 不是 2 个
        self.assertEqual(figs[0].image_bytes, png)       # 只有原图
        self.assertIn("图 1 子图", [p.text for p in doc.all_paragraphs()])
        validate_parsed(doc)


if __name__ == "__main__":
    unittest.main()
