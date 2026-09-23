"""repo 层数据库测试:stdlib sqlite3 内存库即可运行(防参数顺序类回归)。"""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from deephoto import repo
from deephoto.db import init_db, connect
from deephoto.security import ensure_bootstrap_user, resolve_token


class RepoTestBase(unittest.TestCase):
    def setUp(self):
        # thread-local 缓存按路径区分;每个用例独立临时库
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        ensure_bootstrap_user(self.conn, "tenant_a", "admin", "tok_a")
        ensure_bootstrap_user(self.conn, "tenant_b", "admin", "tok_b")
        self.ctx_a = resolve_token(self.conn, "tok_a")
        self.ctx_b = resolve_token(self.conn, "tok_b")

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_doc(self, ctx, filename="a.pdf"):
        return repo.insert_document(
            self.conn, tenant_id=ctx.tenant_id, owner_id=ctx.user_id,
            filename=filename, pdf_object_key="pdfs/00/x.pdf",
            sha256="0" * 64, ingestion_version="v1",
        )


class OwnershipTest(RepoTestBase):
    def test_owned_document_found(self):
        doc_id = self._insert_doc(self.ctx_a)
        doc = repo.get_owned_document(self.conn, doc_id, self.ctx_a.tenant_id)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["id"], doc_id)

    def test_wrong_tenant_not_found(self):
        doc_id = self._insert_doc(self.ctx_a)
        self.assertIsNone(repo.get_owned_document(self.conn, doc_id, self.ctx_b.tenant_id))

    def test_unknown_id_not_found(self):
        self.assertIsNone(repo.get_owned_document(self.conn, "doc_none", self.ctx_a.tenant_id))

    def test_list_is_tenant_scoped(self):
        self._insert_doc(self.ctx_a, "a1.pdf")
        self._insert_doc(self.ctx_b, "b1.pdf")
        self.assertEqual(len(repo.list_documents(self.conn, self.ctx_a.tenant_id)), 1)
        self.assertEqual(len(repo.list_documents(self.conn, self.ctx_b.tenant_id)), 1)


class DeleteTest(RepoTestBase):
    def test_delete_removes_document(self):
        doc_id = self._insert_doc(self.ctx_a)
        result = repo.delete_document(self.conn, doc_id, self.ctx_a.tenant_id)
        self.assertIsNotNone(result)
        self.assertIsNone(repo.get_owned_document(self.conn, doc_id, self.ctx_a.tenant_id))
        self.assertEqual(repo.list_documents(self.conn, self.ctx_a.tenant_id), [])

    def test_delete_wrong_tenant_returns_none(self):
        doc_id = self._insert_doc(self.ctx_a)
        self.assertIsNone(repo.delete_document(self.conn, doc_id, self.ctx_b.tenant_id))
        # 原租户的文档不受影响
        self.assertIsNotNone(repo.get_owned_document(self.conn, doc_id, self.ctx_a.tenant_id))

    def test_delete_unknown_returns_none(self):
        self.assertIsNone(repo.delete_document(self.conn, "doc_none", self.ctx_a.tenant_id))


class DedupLookupTest(RepoTestBase):
    def test_find_ready_by_hash(self):
        doc_id = self._insert_doc(self.ctx_a)
        # 未 ready 时不命中
        self.assertIsNone(repo.find_ready_document_by_hash(self.conn, self.ctx_a.tenant_id, "0" * 64, "v1"))
        repo.update_document_status(self.conn, doc_id, "ready")
        hit = repo.find_ready_document_by_hash(self.conn, self.ctx_a.tenant_id, "0" * 64, "v1")
        self.assertEqual(hit["id"], doc_id)
        # 其他租户/其他版本不命中
        self.assertIsNone(repo.find_ready_document_by_hash(self.conn, self.ctx_b.tenant_id, "0" * 64, "v1"))
        self.assertIsNone(repo.find_ready_document_by_hash(self.conn, self.ctx_a.tenant_id, "0" * 64, "v2"))


if __name__ == "__main__":
    unittest.main()
