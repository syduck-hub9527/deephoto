import io
import json
import unittest
import zipfile

import _bootstrap  # noqa: F401
from deephoto.parsing import pdf_backend
from deephoto.parsing.mineru import (
    MinerUClient,
    MinerUError,
    _content_list_to_pages,
    _zip_to_page_texts,
)


def _make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


class _Response:
    def __init__(self, payload, is_json=True):
        self.payload = json.dumps(payload).encode("utf-8") if is_json else payload

    def read(self):
        return self.payload


class ContentListPagingTest(unittest.TestCase):
    def test_groups_by_page_idx(self):
        items = [
            {"type": "text", "text": "第一章", "page_idx": 0},
            {"type": "text", "text": "正文A", "page_idx": 0},
            {"type": "text", "text": "第二页", "page_idx": 1},
        ]
        self.assertEqual(_content_list_to_pages(items), ["第一章\n正文A", "第二页"])

    def test_fills_missing_pages_with_empty(self):
        items = [{"type": "text", "text": "第三页", "page_idx": 2}]
        self.assertEqual(_content_list_to_pages(items), ["", "", "第三页"])

    def test_zip_prefers_content_list_over_full_md(self):
        zb = _make_zip({
            "x_content_list.json": json.dumps(
                [{"type": "text", "text": "p0", "page_idx": 0}]),
            "full.md": "不应该用这个",
        })
        self.assertEqual(_zip_to_page_texts(zb), ["p0"])

    def test_zip_falls_back_to_full_md(self):
        zb = _make_zip({"full.md": "整篇文本"})
        self.assertEqual(_zip_to_page_texts(zb), ["整篇文本"])


class MinerUClientFlowTest(unittest.TestCase):
    """用注入的 opener 走完 申请->轮询->下载 流程(上传走 http.client,单独 mock)。"""

    def test_parse_pdf_end_to_end(self):
        content = _make_zip({
            "x_content_list.json": json.dumps([
                {"type": "text", "text": "第一页", "page_idx": 0},
                {"type": "text", "text": "第二页", "page_idx": 1},
            ]),
        })
        calls = {"n": 0}

        def opener(request, timeout):
            calls["n"] += 1
            url = request.full_url
            if "file-urls/batch" in url:
                return _Response({"success": True, "data": {
                    "batch_id": "b1", "file_urls": ["https://oss/upload"]}})
            if "extract-results" in url:
                return _Response({"success": True, "data": {
                    "extract_result": [{"state": "done", "full_zip_url": "https://oss/zip"}]}})
            if url == "https://oss/zip":
                return _Response(content, is_json=False)
            raise AssertionError("unexpected url " + url)

        client = MinerUClient(api_key="tok", opener=opener, sleep=lambda s: None)
        # 绕过真实 HTTP 上传与拆分(单块直通)
        client._upload_pdf = lambda url, data: None
        import deephoto.parsing.mineru as m
        orig = m.pdf_backend.chunk_pdf
        m.pdf_backend.chunk_pdf = lambda b, n: [b]
        try:
            result = client.parse_pdf(b"%PDF fake", "t.pdf")
        finally:
            m.pdf_backend.chunk_pdf = orig
        self.assertEqual(result.page_texts, ["第一页", "第二页"])

    def test_failed_state_raises(self):
        def opener(request, timeout):
            url = request.full_url
            if "file-urls/batch" in url:
                return _Response({"success": True, "data": {
                    "batch_id": "b1", "file_urls": ["https://oss/upload"]}})
            return _Response({"success": True, "data": {
                "extract_result": [{"state": "failed", "err_msg": "bad pdf"}]}})

        client = MinerUClient(api_key="tok", opener=opener, sleep=lambda s: None)
        client._upload_pdf = lambda url, data: None
        import deephoto.parsing.mineru as m
        orig = m.pdf_backend.chunk_pdf
        m.pdf_backend.chunk_pdf = lambda b, n: [b]
        try:
            with self.assertRaises(MinerUError):
                client.parse_pdf(b"%PDF fake", "t.pdf")
        finally:
            m.pdf_backend.chunk_pdf = orig

    def test_requires_token(self):
        with self.assertRaises(MinerUError):
            MinerUClient(api_key="  ")


class ChunkedParseTest(unittest.TestCase):
    """多块编排:拆块 -> 逐块解析 -> 按页序合并(网络与上传均 mock)。"""

    def _client_with_chunks(self, chunks, texts_per_chunk):
        client = MinerUClient(api_key="tok", sleep=lambda s: None)
        seen = []

        def fake_parse_chunk(pdf, name):
            seen.append(name)
            idx = chunks.index(pdf)
            return _NR(texts_per_chunk[idx])

        client._parse_chunk = fake_parse_chunk
        # chunk_pdf 由 pdf_backend 提供;这里直接替换避免依赖引擎
        return client, seen

    def test_multi_chunk_merges_in_order(self):
        chunks = [b"chunk0", b"chunk1", b"chunk2"]
        texts = [["p1", "p2"], ["p3", "p4"], ["p5"]]
        client, seen = self._client_with_chunks(chunks, texts)

        import deephoto.parsing.mineru as m
        orig = m.pdf_backend.chunk_pdf
        m.pdf_backend.chunk_pdf = lambda b, n: chunks
        try:
            result = client.parse_pdf(b"%PDF big", "doc.pdf")
        finally:
            m.pdf_backend.chunk_pdf = orig
        self.assertEqual(result.page_texts, ["p1", "p2", "p3", "p4", "p5"])
        self.assertEqual(len(seen), 3)
        # 分块文件名应带 part 后缀
        self.assertTrue(all("part" in n for n in seen))

    def test_single_chunk_uses_filename_as_is(self):
        chunks = [b"only"]
        client, seen = self._client_with_chunks(chunks, [["p1"]])

        import deephoto.parsing.mineru as m
        orig = m.pdf_backend.chunk_pdf
        m.pdf_backend.chunk_pdf = lambda b, n: chunks
        try:
            result = client.parse_pdf(b"%PDF small", "doc.pdf")
        finally:
            m.pdf_backend.chunk_pdf = orig
        self.assertEqual(result.page_texts, ["p1"])
        self.assertEqual(seen, ["doc.pdf"])

    def test_oversize_bytes_rejected(self):
        client = MinerUClient(api_key="tok")
        big = b"x" * (201 * 1024 * 1024)
        with self.assertRaises(MinerUError):
            client.parse_pdf(big, "big.pdf")


def _NR(page_texts):
    from deephoto.parsing.mineru import MinerUResult
    return MinerUResult(page_texts=list(page_texts), raw={})


@unittest.skipUnless(pdf_backend.pymupdf_available(), "拆分 PDF 需要 PyMuPDF")
class ChunkPdfTest(unittest.TestCase):
    def _make_pdf(self, n_pages):
        import pymupdf
        d = pymupdf.open()
        for _ in range(n_pages):
            d.new_page(width=612, height=792)
        raw = d.tobytes()
        d.close()
        return raw

    def test_under_limit_returns_single(self):
        raw = self._make_pdf(5)
        self.assertEqual(len(pdf_backend.chunk_pdf(raw, 200)), 1)

    def test_over_limit_splits_and_preserves_count(self):
        raw = self._make_pdf(5)
        chunks = pdf_backend.chunk_pdf(raw, 2)
        self.assertEqual([self._count(c) for c in chunks], [2, 2, 1])

    def _count(self, raw):
        import pymupdf
        d = pymupdf.open(stream=raw, filetype="pdf")
        n = d.page_count
        d.close()
        return n


if __name__ == "__main__":
    unittest.main()
