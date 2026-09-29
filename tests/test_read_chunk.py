"""search_knowledge 的全文预算与 read_chunk 工具(权限、相邻块、截断标记)。"""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from deephoto import repo
from deephoto.agent.knowledge import KnowledgeService, _FULL_TEXT_BUDGET, _SNIPPET_CHARS
from deephoto.db import connect, init_db
from deephoto.indexing.service import IndexService
from deephoto.security import AuthContext

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


@unittest.skipUnless(HAVE_NUMPY, "需要 numpy(索引服务依赖)")
class ReadChunkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "t.db"
        init_db(db_path)
        self.conn = connect(db_path)
        # 项目已改为无鉴权的固定租户;跨租户隔离仍由 tenant_id 过滤保证,这里直接构造两个上下文
        self.ctx_a = AuthContext(tenant_id="ta", user_id="u")
        self.ctx_b = AuthContext(tenant_id="tb", user_id="u")
        self.doc = repo.insert_document(
            self.conn, tenant_id="ta", owner_id=self.ctx_a.user_id, filename="a.pdf",
            source_object_key="k", sha256="0" * 64, ingestion_version="v1")
        # 5 个块;第 3 个块的关键信息在 320 字之后
        self.ids = []
        for i in range(5):
            filler = "无关铺垫内容。" * 60                     # 约 420 字
            text = f"块{i}:" + filler + ("关键结论:光刻胶被曝光后可溶于显影液。" if i == 2 else "")
            self.ids.append(repo.insert_chunk(
                self.conn, tenant_id="ta", document_id=self.doc, ingestion_version="v1",
                section=None, text=text, page_start=i + 1, page_end=i + 1,
                paragraph_ids=[f"p{i}"], referenced_image_ids=[], nearby_image_ids=[]))
        repo.update_document_status(self.conn, self.doc, "ready", page_count=5)
        self.index = IndexService()
        self.index.upsert_document(self.conn, self.doc)
        self.knowledge = KnowledgeService(store=None, index_service=self.index)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_search_returns_full_text_within_budget(self):
        result = self.knowledge.search(self.conn, self.ctx_a, "光刻胶 显影液")
        hit = next(c for c in result["chunks"] if c["chunk_id"] == self.ids[2])
        self.assertFalse(hit["truncated"])
        self.assertIn("关键结论", hit["text"])          # 过去被 320 字截断,模型看不到

    def test_over_budget_is_marked_truncated(self):
        big = "长" * (_FULL_TEXT_BUDGET + 10)
        repo.insert_chunk(
            self.conn, tenant_id="ta", document_id=self.doc, ingestion_version="v1", section=None,
            text=big + "光刻胶", page_start=6, page_end=6, paragraph_ids=["p9"],
            referenced_image_ids=[], nearby_image_ids=[])
        self.index.upsert_document(self.conn, self.doc)
        result = self.knowledge.search(self.conn, self.ctx_a, "光刻胶")
        truncated = [c for c in result["chunks"] if c["truncated"]]
        self.assertTrue(truncated)
        self.assertLessEqual(len(truncated[0]["text"]), _SNIPPET_CHARS + 1)

    def test_read_chunk_full_text_and_neighbors(self):
        result = self.knowledge.read_chunk(self.conn, self.ctx_a, self.ids[2], neighbors=1)
        self.assertEqual([c["chunk_id"] for c in result["chunks"]], self.ids[1:4])   # 按阅读顺序
        target = next(c for c in result["chunks"] if c["role"] == "target")
        self.assertIn("关键结论", target["text"])

    def test_read_chunk_edges_and_clamp(self):
        first = self.knowledge.read_chunk(self.conn, self.ctx_a, self.ids[0], neighbors=1)
        self.assertEqual([c["chunk_id"] for c in first["chunks"]], self.ids[0:2])
        wide = self.knowledge.read_chunk(self.conn, self.ctx_a, self.ids[2], neighbors=99)
        self.assertEqual(len(wide["chunks"]), 5)          # 上限钳制为 2 -> 前后各 2 块

    def test_cloned_document_keeps_reading_order(self):
        # 去重克隆走 chunks_for_document;相邻块依赖克隆后仍保持阅读顺序
        dst_id = repo.insert_document(
            self.conn, tenant_id="ta", owner_id="u", filename="b.pdf",
            source_object_key="k2", sha256="0" * 64, ingestion_version="v1")
        dst = repo.get_document(self.conn, dst_id)
        chunk_map, _ = repo.clone_document_data(self.conn, src_document_id=self.doc, dst=dst)
        cloned = [c["id"] for c in repo.chunks_in_order(self.conn, dst_id)]
        self.assertEqual(cloned, [chunk_map[i] for i in self.ids])

    def test_read_chunk_denies_other_tenant_and_unknown(self):
        self.assertTrue(self.knowledge.read_chunk(self.conn, self.ctx_b, self.ids[2])["error"])
        self.assertTrue(self.knowledge.read_chunk(self.conn, self.ctx_a, "chk_nope")["error"])

    def test_read_chunk_denies_not_ready_document(self):
        repo.update_document_status(self.conn, self.doc, "indexing")
        self.assertTrue(self.knowledge.read_chunk(self.conn, self.ctx_a, self.ids[2])["error"])


if __name__ == "__main__":
    unittest.main()
