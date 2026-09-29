"""位置文案(locator_label)与 section 类文档的工具输出形态(§3.2/§3.9,§8 位置文案)。"""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from deephoto import repo
from deephoto.agent.knowledge import KnowledgeService
from deephoto.agent.locator import locator_label
from deephoto.db import connect, init_db
from deephoto.indexing.service import IndexService
from deephoto.security import AuthContext

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


class LocatorLabelTest(unittest.TestCase):
    def test_page_kind(self):
        self.assertEqual(locator_label("page", 7), "p.7")
        self.assertEqual(locator_label("page", 1, 2), "p.1–2")
        self.assertEqual(locator_label("page", 4, 4), "p.4")

    def test_slide_kind(self):
        self.assertEqual(locator_label("slide", 3), "幻灯片 3")

    def test_sheet_kind(self):
        self.assertEqual(locator_label("sheet", 2, section="参数表"), "工作表「参数表」")
        self.assertEqual(locator_label("sheet", 2), "工作表 2")

    def test_section_kind(self):
        self.assertEqual(locator_label("section", 5, 5, "第1章 > 1.2 节"), "第1章 > 1.2 节")
        self.assertEqual(locator_label("section", 5), "全文")          # 无章节时不说"第 5 页"


@unittest.skipUnless(HAVE_NUMPY, "需要 numpy(索引服务依赖)")
class SectionDocumentToolOutputTest(unittest.TestCase):
    """locator_kind=section 的文档:工具输出省略 page_start/page_end,给 locator 与文件名。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "t.db"
        init_db(db_path)
        self.conn = connect(db_path)
        self.ctx = AuthContext(tenant_id="ta", user_id="u")
        self.doc = repo.insert_document(
            self.conn, tenant_id="ta", owner_id="u", filename="手册.md",
            source_object_key="k", sha256="1" * 64, ingestion_version="v1",
            source_format="md", locator_kind="section", parse_engine="local")
        self.chunk = repo.insert_chunk(
            self.conn, tenant_id="ta", document_id=self.doc, ingestion_version="v1",
            section="安装 > 依赖", text="光刻胶配置步骤:先安装依赖。", page_start=2, page_end=2,
            paragraph_ids=["p1"], referenced_image_ids=[], nearby_image_ids=[])
        repo.update_document_status(self.conn, self.doc, "ready", page_count=2)
        self.index = IndexService()
        self.index.upsert_document(self.conn, self.doc)
        self.knowledge = KnowledgeService(store=None, index_service=self.index)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_search_omits_virtual_pages_and_gives_locator(self):
        result = self.knowledge.search(self.conn, self.ctx, "光刻胶 配置")
        hit = next(c for c in result["chunks"] if c["chunk_id"] == self.chunk)
        self.assertNotIn("page_start", hit)                  # 虚拟分段号不给模型,避免说"第 2 页"
        self.assertNotIn("page_end", hit)
        self.assertEqual(hit["locator"], "安装 > 依赖")
        self.assertEqual(hit["document"], "手册.md")
        self.assertEqual(hit["source_format"], "md")

    def test_read_chunk_same_shape(self):
        result = self.knowledge.read_chunk(self.conn, self.ctx, self.chunk)
        target = result["chunks"][0]
        self.assertNotIn("page_start", target)
        self.assertEqual(target["locator"], "安装 > 依赖")

    def test_pdf_document_keeps_pages(self):
        # 对照:PDF(page 语义)仍带 page_start/page_end,locator 为 p.N
        doc = repo.insert_document(
            self.conn, tenant_id="ta", owner_id="u", filename="a.pdf",
            source_object_key="k2", sha256="2" * 64, ingestion_version="v1")
        chunk = repo.insert_chunk(
            self.conn, tenant_id="ta", document_id=doc, ingestion_version="v1",
            section=None, text="光刻胶另一种配置。", page_start=3, page_end=4,
            paragraph_ids=["p1"], referenced_image_ids=[], nearby_image_ids=[])
        repo.update_document_status(self.conn, doc, "ready", page_count=9)
        self.index.upsert_document(self.conn, doc)
        result = self.knowledge.read_chunk(self.conn, self.ctx, chunk)
        target = result["chunks"][0]
        self.assertEqual((target["page_start"], target["page_end"]), (3, 4))
        self.assertEqual(target["locator"], "p.3–4")


if __name__ == "__main__":
    unittest.main()
