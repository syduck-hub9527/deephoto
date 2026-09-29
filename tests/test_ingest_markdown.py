"""Markdown 端到端入库:上传字段 -> 本地解析 -> 分块/图文关联 -> 索引 -> 检索(§5 P1 验收)。

不经网络、不经 MinerU;远程图断言零网络请求。
"""

import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401

try:
    import numpy  # noqa: F401
    from PIL import Image
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

if HAVE_DEPS:
    from deephoto import repo
    from deephoto.agent.knowledge import KnowledgeService
    from deephoto.config import Settings
    from deephoto.db import connect, init_db
    from deephoto.indexing.service import IndexService
    from deephoto.pipeline.ingest import IngestService
    from deephoto.security import LOCAL_CTX
    from deephoto.storage import ObjectStore


def _png_b64(color="red") -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


MD = """---
title: 不该进章节
---

# 第1章 存储器

存储器容量持续提升,如图 1.1 所示。

![图 1.1 容量与尺寸](data:image/png;base64,%s)

图 1.1 容量与尺寸

## 1.1 工艺

| 元素 | 类型 |
| --- | --- |
| 磷 | N 型 |

```python
print("code 不入检索结构")
```

![远程图](https://example.com/x.png)
""" % _png_b64()


@unittest.skipUnless(HAVE_DEPS, "需要 numpy 与 pillow")
class IngestMarkdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.settings = Settings(
            moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
            embedding_base_url=None, embedding_api_key=None, embedding_model=None,
            data_dir=root, max_upload_mb=100, ingestion_version="v2", mineru_api_key=None)
        init_db(self.settings.db_path)
        self.conn = connect(self.settings.db_path)
        self.store = ObjectStore(root / "objects")
        key, digest = self.store.put(MD.encode(), "sources", "text/markdown", ext="md")
        self.doc_id = repo.insert_document(
            self.conn, tenant_id=LOCAL_CTX.tenant_id, owner_id=LOCAL_CTX.user_id,
            filename="手册.md", source_object_key=key, sha256=digest, ingestion_version="v2",
            source_format="md", locator_kind="section", parse_engine="local")
        self.index = IndexService()
        # 无 mineru_client_factory:本地解析不应触碰 MinerU;urlopen 拦截证明零网络请求
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("不得发起网络请求")):
            IngestService(self.settings, self.store, parser=None,
                          index_service=self.index).ingest(self.doc_id)
        self.knowledge = KnowledgeService(self.store, self.index)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_ready_with_sections_and_no_front_matter(self):
        doc = repo.get_document(self.conn, self.doc_id)
        self.assertEqual(doc["status"], "ready")
        self.assertEqual(doc["source_format"], "md")
        chunks = repo.chunks_for_document(self.conn, self.doc_id)
        self.assertTrue(chunks)
        sections = {c["section"] for c in chunks}
        self.assertIn("第1章 存储器", sections)
        self.assertIn("第1章 存储器 > 1.1 工艺", sections)
        self.assertNotIn("不该进章节", " ".join(s or "" for s in sections))

    def test_figure_persisted_with_caption_and_remote_image_skipped(self):
        occs = repo.occurrences_for_document(self.conn, self.doc_id)
        self.assertEqual(len(occs), 1)                       # data URI 图入库;远程图只留 alt
        self.assertEqual(occs[0]["figure_number"], "1.1")
        self.assertEqual(occs[0]["bbox_coord"], "none")      # 非 PDF 无坐标系
        asset = repo.get_asset(self.conn, occs[0]["image_asset_id"])
        self.assertEqual(asset["mime_type"], "image/png")
        # 远程图 alt 文本仍在正文里
        texts = "\n".join(c["text"] for c in repo.chunks_for_document(self.conn, self.doc_id))
        self.assertIn("远程图", texts)

    def test_search_and_locator_for_section_document(self):
        result = self.knowledge.search(self.conn, LOCAL_CTX, "容量 尺寸 图")
        self.assertTrue(result["chunks"])
        for c in result["chunks"]:
            self.assertNotIn("page_start", c)                # section 文档不暴露虚拟页码
            self.assertIn("第1章", c["locator"])
        self.assertTrue(any(i["figure_number"] == "1.1" for i in result["images"]))

    def test_page_preview_404_for_md(self):
        # 非 PDF 不提供页预览(路由层语义;此处直接验证文档标记)
        doc = repo.get_document(self.conn, self.doc_id)
        self.assertEqual(doc["locator_kind"], "section")

    def test_dedup_same_engine_reuse_and_cross_engine_miss(self):
        # 同内容同引擎 -> 命中复用;parse_engine 不同 -> 不命中
        hit = repo.find_ready_document_by_hash(
            self.conn, LOCAL_CTX.tenant_id, ObjectStore.sha256_of(MD.encode()), "v2", "local")
        self.assertEqual(hit["id"], self.doc_id)
        miss = repo.find_ready_document_by_hash(
            self.conn, LOCAL_CTX.tenant_id, ObjectStore.sha256_of(MD.encode()), "v2", "mineru")
        self.assertIsNone(miss)


if __name__ == "__main__":
    unittest.main()
