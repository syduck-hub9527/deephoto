"""前端内置的 KaTeX 必须能被应用直接提供(离线可用,公式渲染依赖它)。"""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

from test_upload_route import _settings


@unittest.skipUnless(HAVE_FASTAPI, "需要 fastapi")
class VendorStaticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        from deephoto.api.app import create_app
        self.client = TestClient(create_app(_settings(Path(self.tmp.name))),
                                 raise_server_exceptions=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_katex_assets_served(self):
        js = self.client.get("/vendor/katex/katex.min.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("renderToString", js.text)
        css = self.client.get("/vendor/katex/katex.min.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("KaTeX_Main", css.text)
        font = self.client.get("/vendor/katex/fonts/KaTeX_Main-Regular.woff2")
        self.assertEqual(font.status_code, 200)
        self.assertGreater(len(font.content), 1000)

    def test_index_loads_katex(self):
        html = self.client.get("/").text
        self.assertIn("/vendor/katex/katex.min.css", html)
        self.assertIn("/vendor/katex/katex.min.js", html)

    def test_no_path_traversal(self):
        # 客户端会把字面 ../ 规整掉,所以用编码形式直达服务端的路径解析
        for path in ("/vendor/%2e%2e/segments.js", "/vendor/katex/%2e%2e/%2e%2e/segments.js",
                     "/vendor/katex/..%2f..%2fsegments.js"):
            self.assertNotEqual(self.client.get(path).status_code, 200, path)

    def test_m0_vendor_libs_served(self):
        # M0 技术基座:preact/htm/markdown-it 以 ES Module 直接提供(import map 依赖正确的 JS MIME)
        for path, hint in (
            ("/vendor/preact/preact.mjs", "createElement"),
            ("/vendor/preact/hooks.mjs", "useState"),
            ("/vendor/htm/htm.module.js", "export default"),
            ("/vendor/markdown-it/markdown-it.esm.min.mjs", "markdown-it"),
            ("/vendor/markdown-it/markdown-it.umd.min.js", "markdownit"),
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertIn(hint, resp.text, path)
            self.assertIn("javascript", resp.headers["content-type"], path)

    def test_m0_probe_page_served(self):
        # M0 验证页(临时,M1 落地后连同路由一起删除)
        html = self.client.get("/probe.html")
        self.assertEqual(html.status_code, 200)
        self.assertIn('"preact"', html.text)          # import map
        self.assertIn("/probe.js", html.text)
        js = self.client.get("/probe.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("text/javascript", js.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
