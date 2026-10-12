"""嵌入迁移 BGE-M3(SiliconFlow)测试:md文档/embedding-bge-m3-migration.md §7.1。

假嵌入客户端 + 真实 sqlite;不访问真实 API。openai SDK 的请求参数经
patch.object(emb.client, "create") 捕获,返回 model_dump 字典形式的假响应。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

import numpy as np

from deephoto import repo
from deephoto.config import Settings, load_settings
from deephoto.db import connect, init_db
from deephoto.indexing import reembed
from deephoto.indexing.probe import build_index_service, probe_embeddings
from deephoto.indexing.service import IndexService
from deephoto.llm import OrderedEmbeddings, build_embeddings


def _settings(td: str, **overrides) -> Settings:
    kwargs = dict(
        moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
        embedding_base_url="https://api.siliconflow.cn/v1", embedding_api_key="sk-test",
        embedding_model="BAAI/bge-m3",
        data_dir=Path(td), max_upload_mb=100, ingestion_version="v1", mineru_api_key=None,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class _Resp:
    """openai embeddings.create 的假返回(适配层经 model_dump 读取)。"""

    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return {"data": self._data}


def _fake_create(captured: list, dim: int = 4, shuffle: bool = False):
    """捕获请求参数并返回按 index 标号向量的假 create;shuffle=True 时 data 乱序(S6)。"""

    def create(*, model, input, encoding_format=None, **kw):
        captured.append({"model": model, "input": list(input),
                         "encoding_format": encoding_format, **kw})
        data = [{"index": i, "embedding": [float(i)] + [0.0] * (dim - 1)}
                for i in range(len(input))]
        if shuffle:
            data = list(reversed(data))
        return _Resp(data)

    return create


class _FakeEmbeddings:
    """服务级测试用的确定性假嵌入:查询向量固定,文档向量按文本长度派生。"""

    def __init__(self, query_vec=(1.0, 0.0, 0.0, 0.0), fail: bool = False):
        self.query_vec = list(query_vec)
        self.fail = fail
        self.doc_calls: list[list[str]] = []

    def embed_query(self, text):
        if self.fail:
            raise RuntimeError("query boom")
        return list(self.query_vec)

    def embed_documents(self, texts):
        if self.fail:
            raise RuntimeError("batch boom")
        self.doc_calls.append(list(texts))
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class BuildEmbeddingsTest(unittest.TestCase):
    """§7.1-1/2:build_embeddings 参数与请求体。"""

    def test_client_params_match_settings(self):
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(td, embedding_batch_size=16, embedding_max_retries=5,
                                 embedding_timeout_seconds=30.0)
            emb = build_embeddings(settings)
        self.assertIsInstance(emb, OrderedEmbeddings)
        self.assertEqual(emb._batch_size, 16)
        self.assertEqual(emb._client.max_retries, 5)
        self.assertEqual(emb._client.timeout, 30.0)
        self.assertEqual(emb._client.api_key, "sk-test")

    def test_api_key_falls_back_to_not_needed(self):
        with tempfile.TemporaryDirectory() as td:
            emb = build_embeddings(_settings(td, embedding_api_key=None))
        self.assertEqual(emb._client.api_key, "not-needed")   # 自建端点可能不校验 key

    def test_request_body_batches_model_and_encoding_format(self):
        with tempfile.TemporaryDirectory() as td:
            emb = build_embeddings(_settings(td, embedding_batch_size=20))
        captured: list = []
        with patch.object(emb.client, "create", _fake_create(captured)):
            vectors = emb.embed_documents([f"文本 {i}" for i in range(40)])
        self.assertEqual(len(vectors), 40)
        self.assertEqual(len(captured), 2)                    # 40 条按 20 分两批
        for call in captured:
            self.assertLessEqual(len(call["input"]), 32)      # SiliconFlow 数组上限(S2)
            self.assertEqual(call["model"], "BAAI/bge-m3")
            self.assertEqual(call["encoding_format"], "float")          # D2
            self.assertNotIn("dimensions", call)                        # D4/S3:bge-m3 不传

    def test_embed_query_single_call(self):
        with tempfile.TemporaryDirectory() as td:
            emb = build_embeddings(_settings(td))
        captured: list = []
        with patch.object(emb.client, "create", _fake_create(captured)):
            vec = emb.embed_query("探测")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["input"], ["探测"])
        self.assertEqual(len(vec), 4)

    def test_unconfigured_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(td, embedding_base_url=None, embedding_model=None)
            self.assertIsNone(build_embeddings(settings))


class OrderingTest(unittest.TestCase):
    """§7.1-3:data 乱序返回时按 index 归位,向量与输入顺序一致(R2/S6)。"""

    def test_shuffled_data_is_reordered(self):
        with tempfile.TemporaryDirectory() as td:
            emb = build_embeddings(_settings(td))
        captured: list = []
        with patch.object(emb.client, "create", _fake_create(captured, shuffle=True)):
            vectors = emb.embed_documents(["甲", "乙", "丙"])
        self.assertEqual([v[0] for v in vectors], [0.0, 1.0, 2.0])   # index 藏在向量首维


class ProbeTest(unittest.TestCase):
    """§7.1-4:启动探测(版本号含实测维度;维度不符与异常都降级,不含密钥)。"""

    def test_ok_with_measured_dim_and_version(self):
        class E:
            def embed_query(self, text):
                return [0.0] * 1024

        with tempfile.TemporaryDirectory() as td:
            probe = probe_embeddings(E(), _settings(td))
        self.assertTrue(probe.ok)
        self.assertEqual(probe.dim, 1024)
        self.assertEqual(probe.version, "BAAI/bge-m3#1024")   # D5:维度来自实测,不写死

    def test_expected_dim_mismatch_disables(self):
        class E:
            def embed_query(self, text):
                return [0.0] * 1024

        with tempfile.TemporaryDirectory() as td:
            probe = probe_embeddings(E(), _settings(td, embedding_dim=768))
        self.assertFalse(probe.ok)
        self.assertIn("768", probe.error)
        self.assertIn("1024", probe.error)

    def test_exception_degrades_without_leaking_key(self):
        class E:
            def embed_query(self, text):
                raise RuntimeError("401 Unauthorized: Bearer sk-abc123SECRET refused")

        with tempfile.TemporaryDirectory() as td:
            probe = probe_embeddings(E(), _settings(td, embedding_api_key="sk-abc123SECRET"))
        self.assertFalse(probe.ok)
        self.assertNotIn("sk-abc123SECRET", probe.error)      # 脱敏(§5.2)
        self.assertIn("401", probe.error)

    def test_build_index_service_probe_failure_is_keyword_only(self):
        class Bad:
            def embed_query(self, text):
                raise RuntimeError("connect refused")

        with tempfile.TemporaryDirectory() as td:
            with patch("deephoto.llm.build_embeddings", return_value=Bad()):
                service = build_index_service(_settings(td))
        self.assertIsNone(service.embeddings)
        self.assertIsNone(service.embedding_version)

    def test_build_index_service_ok_carries_version_and_batch_size(self):
        class Good:
            def embed_query(self, text):
                return [0.0] * 4

        with tempfile.TemporaryDirectory() as td:
            with patch("deephoto.llm.build_embeddings", return_value=Good()):
                service = build_index_service(_settings(td, embedding_model="m",
                                                        embedding_batch_size=7))
        self.assertIs(service.embeddings.__class__, Good)
        self.assertEqual(service.embedding_version, "m#4")
        self.assertEqual(service.batch_size, 7)

    def test_build_index_service_unconfigured(self):
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(td, embedding_base_url=None, embedding_model=None)
            service = build_index_service(settings)
        self.assertIsNone(service.embeddings)
        self.assertIsNone(service.embedding_version)


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _item(self, item_id, version, *, text="语义 正文", status="ready", doc="d1"):
        repo.upsert_index_item(
            self.conn, item_id=item_id, tenant_id="ta", document_id=doc,
            ingestion_version="v1", source_type="chunk", source_id=f"src_{item_id}",
            embedding_version=version, searchable_text=text, index_status=status)

    def _vector(self, item_id, values):
        repo.put_embedding(self.conn, item_id,
                           np.asarray(values, dtype=np.float32).tobytes())


class VectorScoresGuardTest(_DbCase):
    """§7.1-5:_vector_scores 的版本守卫、维度守卫与异常降级。"""

    def _service(self, **kw):
        return IndexService(embeddings=_FakeEmbeddings(**kw), embedding_version="m#4")

    def test_only_same_version_vectors_are_used(self):
        self._item("it_new", "m#4")
        self._vector("it_new", [1, 0, 0, 0])
        self._item("it_old", "old#4")
        self._vector("it_old", [0, 1, 0, 0])                 # 同维度旧版本(R5):不得参与
        scores = self._service()._vector_scores(self.conn, "查询", ["it_new", "it_old"])
        self.assertIn("it_new", scores)
        self.assertNotIn("it_old", scores)

    def test_wrong_dim_vectors_are_filtered_without_raising(self):
        self._item("it_new", "m#4")
        self._vector("it_new", [1, 0, 0, 0])
        self._item("it_baddim", "m#4")
        self._vector("it_baddim", [1, 0, 0])                 # 字节长度 != dim*4:过滤
        with self.assertLogs("deephoto.indexing.service", level="WARNING") as logs:
            scores = self._service()._vector_scores(self.conn, "查询", ["it_new", "it_baddim"])
        self.assertIn("it_new", scores)
        self.assertNotIn("it_baddim", scores)
        self.assertTrue(any("dimension mismatch" in line for line in logs.output))

    def test_query_dim_mismatch_returns_empty_and_warns(self):
        self._item("it_new", "m#4")
        self._vector("it_new", [1, 0, 0, 0])
        service = self._service(query_vec=(1.0, 0.0, 0.0))   # 查询向量 3 维 vs 库中 4 维
        with self.assertLogs("deephoto.indexing.service", level="WARNING") as logs:
            scores = service._vector_scores(self.conn, "查询", ["it_new"])
        self.assertEqual(scores, {})
        self.assertTrue(logs.output)

    def test_underlying_exception_returns_empty(self):
        self._item("it_new", "m#4")
        self._vector("it_new", [1, 0, 0, 0])
        service = self._service()
        with patch("deephoto.indexing.service.np.stack", side_effect=RuntimeError("np boom")):
            with self.assertLogs("deephoto.indexing.service", level="ERROR"):
                scores = service._vector_scores(self.conn, "查询", ["it_new"])
        self.assertEqual(scores, {})                          # 不抛,降级关键词

    def test_search_falls_back_to_keyword_results(self):
        self._item("it_new", "m#4", text="语义 检索 正文")
        self._vector("it_new", [1, 0, 0, 0])
        service = self._service(fail=True)                    # embed_query 抛异常
        result = service.search(self.conn, document_ids=["d1"], query="语义")
        self.assertEqual([c["item_id"] for c in result["chunks"]], ["it_new"])   # BM25 兜底


class CloneGuardTest(_DbCase):
    """§7.1-6:克隆只复制版本一致的向量;不一致项置 keyword_only 等待重建。"""

    def test_clone_copies_only_same_version_vectors(self):
        self._item("it_old", "old#4", doc="d1")
        self._vector("it_old", [1, 0, 0, 0])
        self._item("it_new", "m#4", doc="d1")
        self._vector("it_new", [0, 1, 0, 0])
        dst = {"tenant_id": "ta", "id": "d2", "ingestion_version": "v1"}
        id_map = {"src_it_old": "c2_old", "src_it_new": "c2_new"}
        IndexService(embeddings=_FakeEmbeddings(), embedding_version="m#4").clone_document(
            self.conn, src_document_id="d1", dst=dst, chunk_id_map=id_map, occ_id_map={})
        cloned = {i["source_id"]: i for i in repo.index_items_for_documents(self.conn, ["d2"])}
        old, new = cloned["c2_old"], cloned["c2_new"]
        self.assertEqual(old["index_status"], "keyword_only")        # 等待重建
        self.assertEqual(old["embedding_version"], "old#4")          # 原版本号留痕
        self.assertEqual(new["index_status"], "ready")
        vectors = repo.get_embeddings(self.conn, [old["id"], new["id"]])
        self.assertNotIn(old["id"], vectors)                         # 旧版本向量不复制
        self.assertIn(new["id"], vectors)


class ReembedTest(_DbCase):
    """§7.1-7:重建命令(dry-run、失败续跑、prune 门禁、入库中拒绝)。"""

    def setUp(self):
        super().setUp()
        self.settings = _settings(self.tmp.name, embedding_model="m")
        self.db_path = self.settings.db_path                        # main() 使用 settings 的库
        init_db(self.db_path)
        self.conn.close()
        self.conn = connect(self.db_path)

    def _seed(self, count=4):
        for i in range(1, count + 1):
            self._item(f"it_{i}", "old#4", text=f"正文 {i}")
            self._vector(f"it_{i}", [1, 0, 0, 0])
        # 空文本项:本来就不向量化,不进重建(§6 第 5 步 carve-out),旧向量留给 prune
        self._item("it_empty", "old#4", text="")
        self._vector("it_empty", [1, 0, 0, 0])
        self.conn.commit()

    def _run(self, argv, embeddings):
        with patch.object(reembed, "load_settings", return_value=self.settings), \
                patch.object(reembed, "build_embeddings", return_value=embeddings):
            return reembed.main(argv)

    def test_dry_run_writes_nothing(self):
        self._seed()
        code = self._run(["--dry-run"], _FakeEmbeddings())
        self.assertEqual(code, 0)
        self.assertEqual(repo.count_items_missing_vector(self.conn, "m#4"), 4)
        versions = {r["embedding_version"] for r in self.conn.execute(
            "SELECT embedding_version FROM index_items")}
        self.assertEqual(versions, {"old#4"})

    def test_full_rebuild_then_idempotent(self):
        self._seed()
        fake = _FakeEmbeddings()
        self.assertEqual(self._run([], fake), 0)
        rows = {r["id"]: (r["embedding_version"], r["index_status"])
                for r in self.conn.execute("SELECT id, embedding_version, index_status"
                                           " FROM index_items WHERE id != 'it_empty'")}
        self.assertEqual(set(rows.values()), {("m#4", "ready")})
        self.assertEqual(len(repo.get_embeddings(self.conn, list(rows))), 4)
        self.assertEqual(repo.count_items_missing_vector(self.conn, "m#4"), 0)
        calls_before = len(fake.doc_calls)
        self.assertEqual(self._run([], fake), 0)                    # 重跑无 missing,不再调用
        self.assertEqual(len(fake.doc_calls), calls_before)
        empty = self.conn.execute(
            "SELECT embedding_version FROM index_items WHERE id = 'it_empty'").fetchone()
        self.assertEqual(empty["embedding_version"], "old#4")       # 空文本项不动

    def test_failed_batch_is_resumed_on_rerun(self):
        self._seed()
        fake = _FakeEmbeddings()

        original = fake.embed_documents

        def flaky(texts):
            if any("正文 3" in t for t in texts):
                raise RuntimeError("TPM limit reached")
            return original(texts)

        fake.embed_documents = flaky
        self.assertEqual(self._run(["--batch", "2"], fake), 1)      # 第二批失败
        self.assertEqual(repo.count_items_missing_vector(self.conn, "m#4"), 2)
        fake.embed_documents = original
        calls_before = list(fake.doc_calls)
        self.assertEqual(self._run(["--batch", "2"], fake), 0)      # 续跑只处理剩余两项
        new_calls = fake.doc_calls[calls_before.__len__():]
        self.assertEqual(new_calls, [["正文 3", "正文 4"]])
        self.assertEqual(repo.count_items_missing_vector(self.conn, "m#4"), 0)

    def test_prune_only_after_full_success(self):
        self._seed()
        fake = _FakeEmbeddings()

        def always_fail(texts):
            raise RuntimeError("down")

        fake.embed_documents = always_fail
        self.assertEqual(self._run(["--prune"], fake), 1)           # 有失败:不得清理
        self.assertIn("it_empty", repo.get_embeddings(self.conn, ["it_empty"]))
        fake.embed_documents = _FakeEmbeddings().embed_documents
        self.assertEqual(self._run(["--prune"], fake), 0)           # 全部成功才清理
        remaining = repo.get_embeddings(
            self.conn, [f"it_{i}" for i in range(1, 5)] + ["it_empty"])
        self.assertEqual(sorted(remaining), [f"it_{i}" for i in range(1, 5)])   # 旧版本向量已删

    def test_refuses_while_ingestion_in_flight(self):
        self._seed()
        doc = repo.insert_document(self.conn, tenant_id="ta", owner_id="u", filename="x.pdf",
                                   source_object_key="k", sha256="0" * 64,
                                   ingestion_version="v1")
        repo.update_document_status(self.conn, doc, "parsing")
        self.conn.commit()
        fake = _FakeEmbeddings()
        self.assertEqual(self._run([], fake), 2)                    # 入库中:拒绝
        self.assertEqual(fake.doc_calls, [])
        self.assertEqual(self._run(["--force"], fake), 0)           # --force 覆盖

    def test_probe_failure_exits_nonzero(self):
        self._seed()
        self.assertEqual(self._run([], _FakeEmbeddings(fail=True)), 2)


_EMB_ENV_VARS = [
    "DEEPHOTO_EMBEDDING_BASE_URL", "DEEPHOTO_EMBEDDING_API_KEY", "DEEPHOTO_EMBEDDING_MODEL",
    "DEEPHOTO_EMBEDDING_DIM", "DEEPHOTO_EMBEDDING_BATCH_SIZE",
    "DEEPHOTO_EMBEDDING_MAX_RETRIES", "DEEPHOTO_EMBEDDING_TIMEOUT_SECONDS",
]


class ConfigValidationTest(unittest.TestCase):
    """§7.1-8:新配置项的默认、上下界与 Base URL 形态。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _EMB_ENV_VARS}
        for key in _EMB_ENV_VARS:
            os.environ.pop(key, None)
        self._dotenv = patch("deephoto.config._load_dotenv")
        self._dotenv.start()                                        # 隔离仓库根 .env

    def tearDown(self):
        self._dotenv.stop()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _load(self, **env):
        with patch.dict(os.environ, env):
            return load_settings()

    def test_defaults(self):
        s = self._load()
        self.assertIsNone(s.embedding_dim)
        self.assertEqual(s.embedding_batch_size, 20)
        self.assertEqual(s.embedding_max_retries, 3)
        self.assertEqual(s.embedding_timeout_seconds, 60.0)

    def test_batch_size_bounds(self):
        for raw in ("0", "33", "abc"):                              # 1–32(S2)
            with self.assertRaisesRegex(ValueError, "DEEPHOTO_EMBEDDING_BATCH_SIZE"):
                self._load(DEEPHOTO_EMBEDDING_BATCH_SIZE=raw)

    def test_dim_zero_rejected(self):
        with self.assertRaisesRegex(ValueError, "DEEPHOTO_EMBEDDING_DIM"):
            self._load(DEEPHOTO_EMBEDDING_DIM="0")
        self.assertEqual(self._load(DEEPHOTO_EMBEDDING_DIM="1024").embedding_dim, 1024)

    def test_timeout_and_retries_bounds(self):
        with self.assertRaisesRegex(ValueError, "DEEPHOTO_EMBEDDING_TIMEOUT_SECONDS"):
            self._load(DEEPHOTO_EMBEDDING_TIMEOUT_SECONDS="1")      # 下限 5
        with self.assertRaisesRegex(ValueError, "DEEPHOTO_EMBEDDING_MAX_RETRIES"):
            self._load(DEEPHOTO_EMBEDDING_MAX_RETRIES="-1")
        s = self._load(DEEPHOTO_EMBEDDING_TIMEOUT_SECONDS="30", DEEPHOTO_EMBEDDING_MAX_RETRIES="0")
        self.assertEqual((s.embedding_timeout_seconds, s.embedding_max_retries), (30.0, 0))

    def test_base_url_shape(self):
        with self.assertRaisesRegex(ValueError, "DEEPHOTO_EMBEDDING_BASE_URL"):
            self._load(DEEPHOTO_EMBEDDING_BASE_URL="api.siliconflow.cn/v1")     # 缺协议
        for tail in ("/embeddings", "/embeddings/"):
            with self.assertRaisesRegex(ValueError, "DEEPHOTO_EMBEDDING_BASE_URL"):
                self._load(DEEPHOTO_EMBEDDING_BASE_URL=f"https://api.siliconflow.cn/v1{tail}")
        s = self._load(DEEPHOTO_EMBEDDING_BASE_URL="https://api.siliconflow.cn/v1")
        self.assertEqual(s.embedding_base_url, "https://api.siliconflow.cn/v1")


class RegressionTest(_DbCase):
    """§7.1-9:未配置 EMBEDDING_* 时行为与改动前一致(纯关键词,全部 keyword_only)。"""

    def test_keyword_only_without_embeddings(self):
        doc = repo.insert_document(self.conn, tenant_id="ta", owner_id="u", filename="a.pdf",
                                   source_object_key="k", sha256="1" * 64, ingestion_version="v1")
        repo.update_document_status(self.conn, doc, "ready", page_count=3)
        repo.insert_chunk(self.conn, tenant_id="ta", document_id=doc, ingestion_version="v1",
                          section="绪论", text="语义 检索 回归", page_start=1, page_end=1,
                          paragraph_ids=[], referenced_image_ids=[], nearby_image_ids=[])
        self.conn.commit()
        service = build_index_service(_settings(self.tmp.name, embedding_base_url=None,
                                                embedding_model=None))
        self.assertIsNone(service.embeddings)
        self.assertIsNone(service.embedding_version)
        service.upsert_document(self.conn, doc)
        items = repo.index_items_for_documents(self.conn, [doc])
        self.assertEqual([(i["embedding_version"], i["index_status"]) for i in items],
                         [(None, "keyword_only")])
        result = service.search(self.conn, document_ids=[doc], query="语义")
        self.assertEqual(len(result["chunks"]), 1)


if __name__ == "__main__":
    unittest.main()
