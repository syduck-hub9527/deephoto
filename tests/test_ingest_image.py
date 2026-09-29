"""图片降级路径端到端:ImageParser -> 入库 -> 配图条目(P3 §3.4f,无 MinerU)。

不经网络;正文为空(无 OCR),图片靠"图片描述"检索(描述服务未配置时仅告警跳过)。
"""

import io
import tempfile
import unittest
from pathlib import Path

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


def _png(color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


@unittest.skipUnless(HAVE_DEPS, "需要 numpy 与 pillow")
class IngestImageTest(unittest.TestCase):
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
        data = _png()
        key, digest = self.store.put(data, "sources", "image/png", ext="png")
        self.doc_id = repo.insert_document(
            self.conn, tenant_id=LOCAL_CTX.tenant_id, owner_id=LOCAL_CTX.user_id,
            filename="照片.png", source_object_key=key, sha256=digest, ingestion_version="v2",
            source_format="image", locator_kind="page", parse_engine="local")
        self.index = IndexService()
        IngestService(self.settings, self.store, parser=None,
                      index_service=self.index).ingest(self.doc_id)
        self.knowledge = KnowledgeService(self.store, self.index)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_ready_one_page_no_chunks(self):
        doc = repo.get_document(self.conn, self.doc_id)
        self.assertEqual(doc["status"], "ready")
        self.assertEqual(doc["page_count"], 1)
        self.assertEqual(repo.chunks_for_document(self.conn, self.doc_id), [])   # 无正文

    def test_original_image_persisted_as_occurrence(self):
        occs = repo.occurrences_for_document(self.conn, self.doc_id)
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0]["page_number"], 1)
        self.assertEqual(occs[0]["bbox_coord"], "none")
        asset = repo.get_asset(self.conn, occs[0]["image_asset_id"])
        self.assertEqual(asset["mime_type"], "image/png")
        self.assertTrue(self.store.get(asset["original_object_key"]).startswith(b"\x89PNG"))

    def test_image_entry_points_to_original(self):
        # 图片格式有页预览(原图本身,§3.9-6):source_page_url 不为 None
        occs = repo.occurrences_for_document(self.conn, self.doc_id)
        entries = self.knowledge.build_image_entries(
            self.conn, LOCAL_CTX, [o["id"] for o in occs])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_page_url"],
                         f"/api/documents/{self.doc_id}/pages/1")
        self.assertEqual(entries[0]["locator_label"], "p.1")


@unittest.skipUnless(HAVE_DEPS, "需要 numpy 与 pillow")
class IngestImageMineruTest(unittest.TestCase):
    """MinerU 路径(假客户端,返回与真实样本一致的形态:只有 OCR 文本、无图片元素):
    用户问"图里写了什么"命中 OCR 文本块时,原图必须能配出来(whole_document 1.0)。"""

    def setUp(self):
        from deephoto.parsing.content_list import parse_content_list
        from deephoto.parsing.mineru import MinerUResult

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.settings = Settings(
            moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
            embedding_base_url=None, embedding_api_key=None, embedding_model=None,
            data_dir=root, max_upload_mb=100, ingestion_version="v2", mineru_api_key="tok")
        init_db(self.settings.db_path)
        self.conn = connect(self.settings.db_path)
        self.store = ObjectStore(root / "objects")
        data = _png()

        class _FakeClient:      # 真实 MinerU 对图片输入的形态:仅 OCR 文本,无 image 元素
            def parse_file(self, data, filename, fmt):
                items = [{"type": "text", "text": "存储器层次结构 寄存器最快", "page_idx": 0}]
                return MinerUResult(page_texts=["存储器层次结构 寄存器最快"], raw={},
                                    elements=parse_content_list(items, {}))

        key, digest = self.store.put(data, "sources", "image/png", ext="png")
        self.doc_id = repo.insert_document(
            self.conn, tenant_id=LOCAL_CTX.tenant_id, owner_id=LOCAL_CTX.user_id,
            filename="照片.png", source_object_key=key, sha256=digest, ingestion_version="v2",
            source_format="image", locator_kind="page", parse_engine="mineru")
        self.index = IndexService()
        IngestService(self.settings, self.store, parser=None, index_service=self.index,
                      mineru_client_factory=lambda obs: _FakeClient()).ingest(self.doc_id)
        self.knowledge = KnowledgeService(self.store, self.index)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_search_by_ocr_text_returns_original_image(self):
        doc = repo.get_document(self.conn, self.doc_id)
        self.assertEqual(doc["status"], "ready")
        result = self.knowledge.search(self.conn, LOCAL_CTX, "存储器 寄存器")
        self.assertTrue(result["chunks"])
        images = result["images"]
        self.assertEqual(len(images), 1)                   # 修复前:nearby 0.4 不够阈值,为 0
        self.assertEqual(images[0]["relation"], "whole_document")
        self.assertEqual(images[0]["locator_label"], "p.1")


if __name__ == "__main__":
    unittest.main()
