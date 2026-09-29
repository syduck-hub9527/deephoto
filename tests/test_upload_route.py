"""上传路由:格式识别、白名单 415、不符 400、文件名消毒、响应字段(§3.1/§8)。"""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

from deephoto.config import Settings


def _settings(root, **overrides):
    kwargs = dict(
        moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
        embedding_base_url=None, embedding_api_key=None, embedding_model=None,
        data_dir=root, max_upload_mb=100, ingestion_version="v2", mineru_api_key="tok",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@unittest.skipUnless(HAVE_FASTAPI, "需要 fastapi")
class UploadRouteTest(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors:Windows 下路由线程的 sqlite 连接可能还没释放,清理不让其报错
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._conns = []
        from deephoto.api.app import create_app
        self.app = create_app(_settings(Path(self.tmp.name)))
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        for conn in self._conns:
            conn.close()
        self.tmp.cleanup()

    def _conn(self):
        from deephoto.db import connect
        conn = connect(self.app.state.settings.db_path)
        self._conns.append(conn)
        return conn

    def _upload(self, data: bytes, filename: str):
        return self.client.post("/api/documents", files={"file": (filename, data)})

    def test_pdf_accepted(self):
        resp = self._upload(b"%PDF-1.4 fake", "a.pdf")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["source_format"], "pdf")
        from deephoto import repo
        row = repo.get_document(self._conn(), body["document_id"])
        self.assertEqual((row["source_format"], row["locator_kind"], row["parse_engine"]),
                         ("pdf", "page", "mineru"))

    def test_markdown_and_txt_accepted(self):
        for name, key in (("a.md", "md"), ("a.txt", "txt")):
            resp = self._upload("# 标题\n\n正文".encode(), name)
            self.assertEqual(resp.status_code, 200, name)
            self.assertEqual(resp.json()["source_format"], key)

    def test_docx_not_in_default_whitelist(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "x")
        resp = self._upload(buf.getvalue(), "a.docx")
        self.assertEqual(resp.status_code, 415)      # 默认白名单只含 pdf/md/txt

    def test_mismatch_is_400(self):
        resp = self._upload(b"%PDF-1.4 fake", "a.md")
        self.assertEqual(resp.status_code, 400)

    def test_oversize_is_413(self):
        app_settings = self.app.state.settings
        object.__setattr__(app_settings, "max_upload_mb", 0)   # frozen dataclass: 测试内强制
        resp = self._upload(b"%PDF-1.4 fake", "a.pdf")
        self.assertEqual(resp.status_code, 413)

    def test_filename_sanitized(self):
        resp = self._upload(b"%PDF-1.4 fake", "../ev\x07il/报告.pdf")
        self.assertEqual(resp.status_code, 200)
        from deephoto import repo
        row = repo.get_document(self._conn(), resp.json()["document_id"])
        self.assertEqual(row["filename"], "报告.pdf")

    def test_unknown_extension_400(self):
        resp = self._upload(b"hello", "a.xyz")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
