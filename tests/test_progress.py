"""入库观测(进度可见)测试:可注入时钟与假 MinerU/模型,不真实等待(md文档 §14)。

覆盖:正常路径阶段顺序与耗时、排队与处理分离、去重命中、多块解析单项耗时、
慢调用跨连接可见(关键验收)、描述异常与格式失败计数、向量降级、跳过场景、
旧记录兼容、失败与重启中断、观测故障隔离、删除清理与在途事件。
"""

import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
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
    from deephoto.config import Settings
    from deephoto.db import connect, init_db
    from deephoto.indexing.service import IndexService
    from deephoto.parsing.mineru import MinerUResult, _zip_extract
    from deephoto.pipeline import progress as pg
    from deephoto.pipeline.ingest import IngestService
    from deephoto.progress_store import ProgressStore
    from deephoto.security import LOCAL_CTX
    from deephoto.storage import ObjectStore


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="PNG")
    return buf.getvalue()


ITEMS = [
    {"type": "text", "text": "第1章 引论", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "正文段落。", "page_idx": 0},
    {"type": "image", "img_path": "images/a.png", "image_caption": ["图1.1 容量与尺寸"],
     "content": "256MB 0.35μm", "page_idx": 0, "bbox": [0, 0, 500, 500]},
    {"type": "image", "img_path": "images/b.png", "image_caption": ["图1.2 流程"],
     "page_idx": 1, "bbox": [0, 0, 500, 500]},
    {"type": "image", "img_path": "images/c.png", "image_caption": ["图1.3 结构"],
     "page_idx": 1, "bbox": [0, 0, 500, 500]},
]
IMAGES = {name: _png(i) for i, name in enumerate(["images/a.png", "images/b.png", "images/c.png"])}


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x_content_list.json", json.dumps(ITEMS, ensure_ascii=False))
        for name, data in IMAGES.items():
            z.writestr(name, data)
    return buf.getvalue()


class _FakeMinerU:
    """假 MinerU:发出与真实客户端同型的事件,返回真实 ZIP 解析结果。"""

    def __init__(self, on_progress, chunks=1, fail=False):
        self.on_progress = on_progress
        self.chunks = chunks
        self.fail = fail

    def parse_pdf(self, pdf_bytes, filename="document.pdf"):
        e = self.on_progress
        e({"type": "split", "pages": 2, "bytes": len(pdf_bytes), "chunks": self.chunks, "duration_ms": 5})
        if self.fail:
            raise RuntimeError("cloud boom")
        page_texts, elements = [], []
        for i in range(1, self.chunks + 1):
            e({"type": "chunk_start", "index": i, "pages": 2, "bytes": 100})
            e({"type": "request_url_end", "index": i, "duration_ms": 100})
            e({"type": "upload_end", "index": i, "bytes": 100, "duration_ms": 200})
            e({"type": "poll", "index": i, "state": "running", "attempts": 1, "elapsed_ms": 1500})
            e({"type": "poll", "index": i, "state": "done", "attempts": 2, "elapsed_ms": 3000})
            e({"type": "download_end", "index": i, "bytes": 5000, "duration_ms": 300})
            e({"type": "extract_end", "index": i, "duration_ms": 20, "elements": 5,
               "figures": 3, "fallback": False})
            texts, els = _zip_extract(_zip_bytes(), expected_pages=2)
            offset = len(page_texts)
            page_texts.extend(texts)
            from dataclasses import replace
            elements.extend(replace(el, page_idx=el.page_idx + offset) for el in els)
        if self.chunks > 1:
            e({"type": "merge_end", "chunks": self.chunks, "pages": len(page_texts), "duration_ms": 5000})
        return MinerUResult(page_texts=page_texts, raw={}, elements=elements)


class _Clock:
    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t

    def wall(self):
        return 1_700_000_000.0 + self.t

    def sleep(self, seconds):
        self.t += seconds


class _FakeEmbeddings:
    def __init__(self, fail_calls=()):
        self.calls = 0
        self.fail_calls = set(fail_calls)

    def embed_documents(self, texts):
        self.calls += 1
        if self.calls in self.fail_calls:
            raise RuntimeError("embed boom")
        return [[0.1, 0.2]] * len(texts)


@unittest.skipUnless(HAVE_DEPS, "需要 numpy 与 pillow")
class ProgressTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.clock = _Clock()
        self.settings = Settings(
            moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
            embedding_base_url=None, embedding_api_key=None, embedding_model=None,
            data_dir=root, max_upload_mb=100, ingestion_version="v2", mineru_api_key="tok")
        init_db(self.settings.db_path)
        self.conn = connect(self.settings.db_path)
        self.store = ObjectStore(root / "objects")
        self.prog = ProgressStore(root / "progress.db", instance_id="inst-test",
                                  now=self.clock.wall, clock=self.clock.monotonic)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _new_doc(self, content=b"%PDF-1.4 fake", sha=None):
        pdf_key, digest = self.store.put(content, "pdf", "application/pdf")
        return repo.insert_document(
            self.conn, tenant_id=LOCAL_CTX.tenant_id, owner_id=LOCAL_CTX.user_id,
            filename="t.pdf", pdf_object_key=pdf_key, sha256=sha or digest,
            ingestion_version="v2")

    def _ingest(self, doc_id, *, chunks=1, embeddings=None, describe=None, mineru_fail=False,
                advance=0.0, register=True):
        """登记→领取→执行;describe 为假 describe_image(模块级 patch 目标)。"""
        if register:
            run_id = f"run_{doc_id}"          # 每次入库尝试独立 run_id(与真实上传一致)
            self.prog.register_run(run_id, doc_id, LOCAL_CTX.tenant_id)
            self.clock.sleep(2.0)
            self.prog.claim_run(run_id)
        service = IngestService(
            self.settings, self.store, parser=None,
            index_service=IndexService(embeddings=embeddings,
                                       embedding_version="v" if embeddings else None),
            chat_model_factory=lambda: object() if describe else None,
            progress_store=self.prog,
            mineru_client_factory=lambda obs: _FakeMinerU(obs, chunks=chunks, fail=mineru_fail),
        )
        if describe is not None:
            import deephoto.pipeline.ingest as ingest_mod
            original = ingest_mod.describe_image
            ingest_mod.describe_image = describe
            try:
                service.ingest(doc_id)
            finally:
                ingest_mod.describe_image = original
        else:
            service.ingest(doc_id)
        self.clock.sleep(advance)

    def _detail(self, doc_id):
        return self.prog.detail(LOCAL_CTX.tenant_id, doc_id)

    def _stages(self, doc_id):
        return {s["stage"]: s for s in self._detail(doc_id)["stages"]}


GOOD_DESC = lambda *a, **k: {
    "visible_summary": "s", "visible_labels": [], "caption": "c", "context_summary": "",
    "uncertain_details": [], "diagnostic": {"result": "ok"}}


class FullRunTest(ProgressTestBase):
    def test_stages_in_order_with_durations_and_terminal_state(self):
        doc_id = self._new_doc()
        self._ingest(doc_id, describe=GOOD_DESC)
        detail = self._detail(doc_id)
        order = [s["stage"] for s in detail["stages"]]
        for expected in ("queued", "dedup_lookup", "parsing", "mineru_split",
                         "persist_figures", "chunks_and_links", "describing",
                         "indexing", "index_prepare", "embed_batches", "finalizing"):
            self.assertIn(expected, order, expected)
        # 顶层阶段顺序与起止正确;每段有非负耗时
        top = [s for s in detail["stages"] if s["stage"] in pg.TOP_STAGES]
        self.assertEqual([s["stage"] for s in top], list(pg.TOP_STAGES[:2]) + [
            "parsing", "persist_figures", "chunks_and_links", "describing",
            "indexing", "finalizing"])
        for s in top:
            self.assertIsNotNone(s["duration_ms"], s["stage"])
            self.assertEqual(s["result"], "succeeded", s["stage"])
        # 结束状态与业务状态一致
        self.assertEqual(repo.get_document(self.conn, doc_id)["status"], "ready")
        self.assertEqual(detail["summary"]["state"], "succeeded")

    def test_queue_and_processing_separated(self):
        doc_id = self._new_doc()
        self._ingest(doc_id, describe=GOOD_DESC)
        summary = self._detail(doc_id)["summary"]
        self.assertEqual(summary["queue_elapsed_ms"], 2000)          # 登记->领取 2 秒
        self.assertGreaterEqual(summary["processing_elapsed_ms"], 0)
        self.assertGreaterEqual(summary["total_elapsed_ms"], 2000)   # 不混算

    def test_describe_counts_ok(self):
        doc_id = self._new_doc()
        self._ingest(doc_id, describe=GOOD_DESC)
        stages = self._stages(doc_id)
        counts = stages["describing"]["counts"]
        self.assertEqual((counts["completed"], counts["total"], counts["succeeded"]), (3, 3, 3))
        self.assertEqual(counts["failed"], 0)
        images = [i for i in self._detail(doc_id)["items"] if i["kind"] == "image"]
        self.assertEqual(len(images), 3)
        self.assertTrue(all(i["result"] == "ok" for i in images))
        self.assertEqual(images[0]["figure"], "1.1")

    def test_dedup_hit_records_only_lookup_and_reuse(self):
        first = self._new_doc()
        self._ingest(first, describe=GOOD_DESC)
        second = self._new_doc()   # 同内容同版本 -> 命中去重
        self._ingest(second)
        stages = self._stages(second)
        self.assertEqual(stages["dedup_lookup"]["detail"], {"hit": True})
        self.assertEqual(stages["reuse"]["result"], "succeeded")
        self.assertNotIn("parsing", stages)
        self.assertNotIn("describing", stages)     # 不伪造解析/描述耗时
        self.assertEqual(repo.get_document(self.conn, second)["status"], "ready")

    def test_mineru_chunk_items_split_durations(self):
        doc_id = self._new_doc()
        self._ingest(doc_id, chunks=2, describe=GOOD_DESC)
        detail = self._detail(doc_id)
        chunks = [i for i in detail["items"] if i["kind"] == "mineru_chunk"]
        self.assertEqual([c["seq"] for c in chunks], [1, 2])          # 部分序号正确
        for c in chunks:
            d = c["detail"]
            self.assertEqual((d["request_ms"], d["upload_ms"], d["download_ms"]), (100, 200, 300))
            self.assertEqual(d["wait_ms"], 3000)                      # 上传/等待/下载可区分
            self.assertEqual(d["polls"], 2)
        stages = self._stages(doc_id)
        self.assertIn("mineru_merge", stages)                         # 多块有合并子阶段
        # parsing 是独立父级耗时,不是子项求和
        self.assertIsNotNone(stages["parsing"]["duration_ms"])

    def test_describe_error_and_parse_failed_counted(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("model boom")
            if calls["n"] == 2:
                return {"visible_summary": "", "visible_labels": [], "caption": "",
                        "context_summary": "", "uncertain_details": ["bad"],
                        "diagnostic": {"result": "parse_failed"}}
            return GOOD_DESC()

        doc_id = self._new_doc()
        self._ingest(doc_id, describe=flaky)
        stages = self._stages(doc_id)
        counts = stages["describing"]["counts"]
        # 已处理 3/3,成功 1,失败 2(异常 1 + 格式失败 1);失败不算成功
        self.assertEqual((counts["completed"], counts["succeeded"], counts["failed"]), (3, 1, 2))
        self.assertEqual(stages["describing"]["result"], "partial")
        self.assertEqual(repo.get_document(self.conn, doc_id)["status"], "ready")   # 遵循现有继续规则

    def test_skip_scenarios(self):
        doc_id = self._new_doc()
        self._ingest(doc_id, describe=None)   # 无描述模型、无向量
        stages = self._stages(doc_id)
        self.assertEqual(stages["describing"]["result"], "skipped")   # 跳过而非失败
        self.assertEqual(stages["embed_batches"]["result"], "skipped")
        self.assertIn("未启用语义向量", stages["embed_batches"]["detail"]["reason"])
        self.assertEqual(repo.get_document(self.conn, doc_id)["status"], "ready")

    def test_legacy_doc_without_run(self):
        doc_id = self._new_doc()
        docs = [{"id": doc_id, "status": "queued"}]
        self.assertEqual(self.prog.summaries(LOCAL_CTX.tenant_id, docs), {})
        self.assertIsNone(self._detail(doc_id))                       # 不填造零耗时

    def test_failure_terminal_and_interrupt(self):
        doc_id = self._new_doc()
        self._ingest(doc_id, mineru_fail=True)
        self.assertEqual(repo.get_document(self.conn, doc_id)["status"], "failed")
        stages = self._stages(doc_id)
        self.assertEqual(stages["parsing"]["result"], "failed")       # 核心异常有终态
        self.assertEqual(self._detail(doc_id)["summary"]["state"], "failed")

        # 旧实例中断:新实例启动时标记;业务已终态优先
        old = ProgressStore(Path(self.tmp.name) / "p2.db", instance_id="old",
                            now=self.clock.wall, clock=self.clock.monotonic)
        doc2 = self._new_doc(b"%PDF-1.4 other")
        old.register_run("run_old", doc2, LOCAL_CTX.tenant_id)
        old.claim_run("run_old")
        new = ProgressStore(Path(self.tmp.name) / "p2.db", instance_id="new",
                            now=self.clock.wall, clock=self.clock.monotonic)
        new.mark_interrupted()
        summary = new.summaries(LOCAL_CTX.tenant_id, [{"id": doc2, "status": "queued"}])[doc2]
        self.assertEqual(summary["state"], "interrupted")             # 不再伪装活跃
        repo.update_document_status(self.conn, doc2, "ready", page_count=2)
        summary = new.summaries(LOCAL_CTX.tenant_id, [{"id": doc2, "status": "ready"}])[doc2]
        self.assertEqual(summary["state"], "succeeded")               # 业务终态优先

    def test_observer_failure_isolated(self):
        bad = Path(self.tmp.name) / "afile"
        bad.write_text("x")
        broken = ProgressStore(bad / "progress.db", instance_id="bad")   # 父路径是文件,建库必失败
        doc_id = self._new_doc()
        # 观测库不可写不破坏业务,也不引入长时间等待
        service = IngestService(
            self.settings, self.store, parser=None, index_service=IndexService(),
            chat_model_factory=lambda: object(), progress_store=broken,
            mineru_client_factory=lambda obs: _FakeMinerU(obs))
        import deephoto.pipeline.ingest as ingest_mod
        original = ingest_mod.describe_image
        ingest_mod.describe_image = GOOD_DESC
        try:
            service.ingest(doc_id)
        finally:
            ingest_mod.describe_image = original
        self.assertEqual(repo.get_document(self.conn, doc_id)["status"], "ready")

    def test_delete_cleanup_and_late_events_not_recreated(self):
        doc_id = self._new_doc()
        self.prog.register_run("run_1", doc_id, LOCAL_CTX.tenant_id)
        observer = self.prog.observer("run_1")
        observer.stage_start(pg.STAGE_PARSING)
        self.prog.cleanup_documents([doc_id])
        self.assertIsNone(self._detail(doc_id))
        observer.stage_start(pg.STAGE_DESCRIBING)                     # 迟到回调不重建
        observer.item_start("image", 1, label="x")
        self.assertIsNone(self._detail(doc_id))

    def test_tenant_isolation(self):
        doc_id = self._new_doc()
        self.prog.register_run("run_1", doc_id, LOCAL_CTX.tenant_id)
        self.assertEqual(self.prog.summaries("other-tenant", [{"id": doc_id, "status": "queued"}]), {})
        self.assertIsNone(self.prog.detail("other-tenant", doc_id))

    def test_embed_batch_degraded_continues_with_warning(self):
        doc_id = self._new_doc()
        for i in range(21):   # 超过单批 20 条上限 -> 两批
            repo.insert_chunk(self.conn, tenant_id=LOCAL_CTX.tenant_id, document_id=doc_id,
                              ingestion_version="v2", section=None, text=f"块{i}内容",
                              page_start=1, page_end=1, paragraph_ids=[f"p{i}"],
                              referenced_image_ids=[], nearby_image_ids=[])
        repo.update_document_status(self.conn, doc_id, "ready", page_count=1)
        self.prog.register_run("run_1", doc_id, LOCAL_CTX.tenant_id)
        observer = self.prog.observer("run_1")
        embeddings = _FakeEmbeddings(fail_calls={2})
        observer.stage_start(pg.STAGE_INDEXING)
        IndexService(embeddings=embeddings, embedding_version="v").upsert_document(
            self.conn, doc_id, observer=observer)
        observer.stage_end(pg.STAGE_INDEXING)
        stages = self._stages(doc_id)
        self.assertEqual(stages["embed_batches"]["result"], "partial")
        batches = [i for i in self._detail(doc_id)["items"] if i["kind"] == "embed_batch"]
        self.assertEqual([(b["seq"], b["result"], b["count"]) for b in batches],
                         [(1, "ok", 20), (2, "degraded", 1)])
        self.assertTrue(self._detail(doc_id)["summary"]["warnings"])   # 降级警告保留


class SlowCallVisibleTest(ProgressTestBase):
    """关键验收:慢模型调用期间,独立 API 连接仍能读到当前第几张、已开始(§14.5)。"""

    def test_slow_describe_visible_from_another_connection(self):
        doc_id = self._new_doc()
        self.prog.register_run("run_1", doc_id, LOCAL_CTX.tenant_id)
        self.prog.claim_run("run_1")
        gate = threading.Event()
        real_store = ProgressStore(Path(self.tmp.name) / "progress.db", instance_id="inst-test")

        def blocking_describe(*a, **k):
            gate.wait(timeout=30)
            return GOOD_DESC()

        service = IngestService(
            self.settings, self.store, parser=None, index_service=IndexService(),
            chat_model_factory=lambda: object(), progress_store=real_store,
            mineru_client_factory=lambda obs: _FakeMinerU(obs))
        import deephoto.pipeline.ingest as ingest_mod
        original = ingest_mod.describe_image
        ingest_mod.describe_image = blocking_describe
        thread = threading.Thread(target=lambda: service.ingest(doc_id), daemon=True)
        try:
            thread.start()
            # 用另一个连接(模拟 API 线程)轮询,直到看到第 1 张已开始
            deadline = time.time() + 30
            seen = None
            while time.time() < deadline:
                docs = [{"id": doc_id, "status": "describing"}]
                summaries = real_store.summaries(LOCAL_CTX.tenant_id, docs)
                current = (summaries.get(doc_id) or {}).get("current_item")
                if current:
                    seen = (summaries[doc_id], current)
                    break
                time.sleep(0.05)
            self.assertIsNotNone(seen, "慢调用期间读不到当前单项")
            summary, current = seen
            self.assertEqual(current["index"], 1)
            self.assertIsNotNone(current.get("started_at"))
            self.assertEqual(summary["stage"], "describing")
            self.assertEqual(summary["total"], 3)
        finally:
            gate.set()
            ingest_mod.describe_image = original
            thread.join(timeout=30)
        self.assertEqual(repo.get_document(self.conn, doc_id)["status"], "ready")


class LateFixesTest(ProgressTestBase):
    """评审修复回归:轮询计时不归零、拆分/合并真实耗时、失败收尾、迟到回调不抛错。"""

    def test_poll_updates_preserve_original_start(self):
        # 模拟云端等待 120 秒:轮询只更新状态,当前单项已等待仍按原始开始时间累计
        doc_id = self._new_doc()
        self.prog.register_run("run_1", doc_id, LOCAL_CTX.tenant_id)
        observer = self.prog.observer("run_1")
        observer.item_start("mineru_chunk", 1, label="第 1 部分")
        self.clock.sleep(120.0)
        observer.item_update("mineru_chunk", 1, label="第 1 部分 · 云端解析",
                             detail={"last_state": "running", "polls": 40})
        summary = self.prog.summaries(LOCAL_CTX.tenant_id, [{"id": doc_id, "status": "parsing"}])[doc_id]
        self.assertEqual(summary["current_item"]["elapsed_ms"], 120_000)

    def test_split_uses_event_duration_and_restores_parent_stage(self):
        doc_id = self._new_doc()
        self.prog.register_run("run_1", doc_id, LOCAL_CTX.tenant_id)
        observer = self.prog.observer("run_1")
        observer.stage_start(pg.STAGE_PARSING)
        from deephoto.pipeline.ingest import _MinerUProgressAdapter
        adapter = _MinerUProgressAdapter(observer)
        adapter({"type": "split", "pages": 7, "bytes": 1000, "chunks": 1, "duration_ms": 8000})
        stages = self._stages(doc_id)
        self.assertEqual(stages["mineru_split"]["duration_ms"], 8000)     # 事件自带耗时,不记 0
        run = self.prog._latest_run(LOCAL_CTX.tenant_id, doc_id)
        self.assertEqual(run["current_stage"], "parsing")                 # 子阶段结束恢复父级

    def test_failed_run_ends_in_flight_stage_and_item_with_real_duration(self):
        doc_id = self._new_doc()
        self.prog.register_run("run_1", doc_id, LOCAL_CTX.tenant_id)
        observer = self.prog.observer("run_1")
        observer.stage_start(pg.STAGE_DESCRIBING, total=2)
        observer.item_start("image", 1, label="图 1.1")
        self.clock.sleep(42.0)
        observer.finish(pg.RESULT_FAILED)
        detail = self._detail(doc_id)
        stage = {s["stage"]: s for s in detail["stages"]}["describing"]
        self.assertEqual((stage["result"], stage["duration_ms"]), ("failed", 42_000))   # 不填假 0
        item = detail["items"][0]
        self.assertEqual((item["result"], item["duration_ms"]), ("failed", 42_000))     # 单项不再显示进行中

    def test_late_callbacks_after_cleanup_do_not_raise_or_recreate(self):
        doc_id = self._new_doc()
        self.prog.register_run("run_1", doc_id, LOCAL_CTX.tenant_id)
        observer = self.prog.observer("run_1")
        observer.stage_start(pg.STAGE_DESCRIBING, total=1)
        observer.item_start("image", 1, label="图 1.1")
        self.prog.cleanup_documents([doc_id])
        # 单项结束/警告/终态在记录清理后必须安静无副作用(观测不能干扰业务)
        observer.item_end("image", 1, pg.ITEM_ERROR, error_kind="RuntimeError")
        observer.warn("迟到警告")
        observer.stage_end(pg.STAGE_DESCRIBING, pg.RESULT_FAILED)
        observer.finish(pg.RESULT_FAILED)
        self.assertIsNone(self._detail(doc_id))
        count = self.prog._conn().execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_interrupt_marks_items_and_detail_uses_business_terminal(self):
        # 旧实例中断:在途单项一并标记中断(不再"进行中"),未知耗时保持空
        old = ProgressStore(Path(self.tmp.name) / "p3.db", instance_id="old",
                            now=self.clock.wall, clock=self.clock.monotonic)
        doc_id = self._new_doc()
        old.register_run("run_old", doc_id, LOCAL_CTX.tenant_id)
        old.claim_run("run_old")
        observer = old.observer("run_old")
        observer.stage_start(pg.STAGE_DESCRIBING, total=1)
        observer.item_start("image", 1, label="图 1.1")
        new = ProgressStore(Path(self.tmp.name) / "p3.db", instance_id="new",
                            now=self.clock.wall, clock=self.clock.monotonic)
        new.mark_interrupted()
        detail = new.detail(LOCAL_CTX.tenant_id, doc_id, business_status="queued")
        item = detail["items"][0]
        self.assertEqual(item["result"], "interrupted")
        self.assertIsNone(item["duration_ms"])            # 未知耗时显示未知,不虚涨
        # 业务已终态、观测终态未写成功:详情与列表同一套终态判断
        detail = new.detail(LOCAL_CTX.tenant_id, doc_id, business_status="ready")
        self.assertEqual(detail["summary"]["state"], "succeeded")

    def test_merge_end_measures_only_merge_work(self):
        # 多块合并耗时只含拼接操作,不含拆分与各块往返(此前按整个解析计时)
        import time as _time
        from deephoto.parsing.mineru import MinerUClient
        events = []
        client = MinerUClient(api_key="tok", sleep=lambda s: None, on_progress=events.append)
        chunks = [b"c0", b"c1"]

        def slow_chunk(pdf, name):
            _time.sleep(0.2)
            return MinerUResult(page_texts=[f"{name}文本"], raw={}, elements=[])

        client._parse_chunk = slow_chunk
        import deephoto.parsing.mineru as m
        orig = m.pdf_backend.chunk_pdf
        m.pdf_backend.chunk_pdf = lambda b, n, *a, **k: chunks
        try:
            client.parse_pdf(b"%PDF big", "doc.pdf")
        finally:
            m.pdf_backend.chunk_pdf = orig
        merge = next(e for e in events if e["type"] == "merge_end")
        self.assertIsNotNone(merge["duration_ms"])
        self.assertLess(merge["duration_ms"], 200)        # 各块往返共 0.4s,合并只有拼接


if __name__ == "__main__":
    unittest.main()
