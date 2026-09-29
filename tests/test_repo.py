"""repo 层数据库测试:stdlib sqlite3 内存库即可运行(防参数顺序类回归)。"""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from deephoto import repo
from deephoto.db import connect, init_db
from deephoto.security import AuthContext


class RepoTestBase(unittest.TestCase):
    def setUp(self):
        # thread-local 缓存按路径区分;每个用例独立临时库
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.ctx_a = AuthContext(tenant_id="tenant_a", user_id="admin")
        self.ctx_b = AuthContext(tenant_id="tenant_b", user_id="admin")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _insert_doc(self, ctx, filename="a.pdf", parse_engine="mineru"):
        return repo.insert_document(
            self.conn,
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            filename=filename,
            source_object_key="sources/00/x.pdf",
            sha256="0" * 64,
            ingestion_version="v1",
            parse_engine=parse_engine,
        )


class OwnershipTest(RepoTestBase):
    def test_owned_document_found(self):
        doc_id = self._insert_doc(self.ctx_a)
        doc = repo.get_owned_document(self.conn, doc_id, self.ctx_a.tenant_id)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["id"], doc_id)

    def test_wrong_tenant_not_found(self):
        doc_id = self._insert_doc(self.ctx_a)
        self.assertIsNone(
            repo.get_owned_document(self.conn, doc_id, self.ctx_b.tenant_id)
        )

    def test_unknown_id_not_found(self):
        self.assertIsNone(
            repo.get_owned_document(self.conn, "doc_none", self.ctx_a.tenant_id)
        )

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
        self.assertIsNone(
            repo.get_owned_document(self.conn, doc_id, self.ctx_a.tenant_id)
        )
        self.assertEqual(repo.list_documents(self.conn, self.ctx_a.tenant_id), [])

    def test_delete_wrong_tenant_returns_none(self):
        doc_id = self._insert_doc(self.ctx_a)
        self.assertIsNone(repo.delete_document(self.conn, doc_id, self.ctx_b.tenant_id))
        # 原租户的文档不受影响
        self.assertIsNotNone(
            repo.get_owned_document(self.conn, doc_id, self.ctx_a.tenant_id)
        )

    def test_delete_unknown_returns_none(self):
        self.assertIsNone(
            repo.delete_document(self.conn, "doc_none", self.ctx_a.tenant_id)
        )


class DedupLookupTest(RepoTestBase):
    def test_find_ready_by_hash(self):
        doc_id = self._insert_doc(self.ctx_a)
        # 未 ready 时不命中
        self.assertIsNone(
            repo.find_ready_document_by_hash(
                self.conn, self.ctx_a.tenant_id, "0" * 64, "v1", "mineru"
            )
        )
        repo.update_document_status(self.conn, doc_id, "ready")
        hit = repo.find_ready_document_by_hash(
            self.conn, self.ctx_a.tenant_id, "0" * 64, "v1", "mineru"
        )
        self.assertEqual(hit["id"], doc_id)
        # 其他租户/其他版本不命中
        self.assertIsNone(
            repo.find_ready_document_by_hash(
                self.conn, self.ctx_b.tenant_id, "0" * 64, "v1", "mineru"
            )
        )
        self.assertIsNone(
            repo.find_ready_document_by_hash(
                self.conn, self.ctx_a.tenant_id, "0" * 64, "v2", "mineru"
            )
        )

    def test_dedup_key_includes_parse_engine(self):
        # 同一文件切换引擎不命中旧引擎的结果(DEV_multi_format §3.5)
        doc_id = self._insert_doc(self.ctx_a)
        repo.update_document_status(self.conn, doc_id, "ready")
        self.assertIsNone(
            repo.find_ready_document_by_hash(
                self.conn, self.ctx_a.tenant_id, "0" * 64, "v1", "local"
            )
        )
        hit = repo.find_ready_document_by_hash(
            self.conn, self.ctx_a.tenant_id, "0" * 64, "v1", "mineru"
        )
        self.assertEqual(hit["id"], doc_id)


class MigrationTest(unittest.TestCase):
    """旧库(列名 pdf_object_key、无新列)经 init_db 自动迁移;幂等(§8 迁移)。"""

    @staticmethod
    def _build_old_db(db_path: Path) -> None:
        import sqlite3
        raw = sqlite3.connect(str(db_path))
        raw.execute(
            "CREATE TABLE documents (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,"
            " owner_id TEXT NOT NULL, filename TEXT NOT NULL, pdf_object_key TEXT NOT NULL,"
            " sha256 TEXT NOT NULL, status TEXT NOT NULL, error TEXT,"
            " ingestion_version TEXT NOT NULL, page_count INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL)")
        raw.execute(
            "INSERT INTO documents (id, tenant_id, owner_id, filename, pdf_object_key,"
            " sha256, status, ingestion_version, created_at)"
            " VALUES ('doc_old', 'tenant_a', 'admin', 'a.pdf', 'pdfs/00/x.pdf',"
            " 'aaaa', 'ready', 'v1', '2026-01-01T00:00:00Z')")
        raw.commit()
        raw.close()

    def test_old_db_migrated_with_defaults_and_idempotent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "old.db"
            self._build_old_db(db_path)

            init_db(db_path)
            init_db(db_path)   # 重复执行幂等
            conn = connect(db_path)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
            self.assertNotIn("pdf_object_key", columns)
            for col in ("source_object_key", "source_format", "locator_kind",
                        "parse_engine", "source_meta"):
                self.assertIn(col, columns)
            doc = repo.get_document(conn, "doc_old")
            self.assertEqual(doc["source_object_key"], "pdfs/00/x.pdf")
            self.assertEqual((doc["source_format"], doc["locator_kind"], doc["parse_engine"]),
                             ("pdf", "page", "mineru"))
            self.assertIsNone(doc["source_meta"])
            # 迁移后的旧文档可命中去重(回填 mineru)
            hit = repo.find_ready_document_by_hash(conn, "tenant_a", "aaaa", "v1", "mineru")
            self.assertEqual(hit["id"], "doc_old")
            conn.close()

    def test_rename_requires_sqlite_325_readable_error(self):
        # RENAME COLUMN 需要 SQLite ≥ 3.25;版本不足时给可读报错,不抛底层 OperationalError
        from unittest import mock
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "old.db"
            self._build_old_db(db_path)
            with mock.patch("sqlite3.sqlite_version_info", (3, 24, 0)):
                with self.assertRaisesRegex(RuntimeError, "SQLite ≥ 3.25"):
                    init_db(db_path)
            # 版本足够时重跑同一库可恢复(迁移步骤按列存在判断,幂等)
            init_db(db_path)
            conn = connect(db_path)
            self.assertEqual(repo.get_document(conn, "doc_old")["source_object_key"],
                             "pdfs/00/x.pdf")
            conn.close()


if __name__ == "__main__":
    unittest.main()
