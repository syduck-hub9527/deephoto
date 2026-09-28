"""正文内嵌图片的后端协议测试:假模型/假工具证据,不依赖真实付费 API。

覆盖:显式锚点保留 occurrence 元数据(同资产不互相覆盖)、编造/越权 ID 被丢弃、
顺序与正文标记一致、重复锚点去重、无锚点时看图兜底仍有效、非流式与 SSE done 一致。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401

from deephoto import repo
from deephoto.agent.qa import QAService
from deephoto.agent.knowledge import KnowledgeService
from deephoto.db import connect, init_db
from deephoto.security import AuthContext


def _make_occ(conn, doc, tenant, asset, page, figure_number):
    return repo.insert_occurrence(
        conn, tenant_id=tenant, document_id=doc, ingestion_version="v1",
        image_asset_id=asset, page_number=page, bbox=None, figure_number=figure_number,
        caption=f"图注 p{page}", extraction_method="parser_image", needs_review=False)


class InlineImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.ctx = AuthContext(tenant_id="ta", user_id="u")
        self.other = AuthContext(tenant_id="tb", user_id="u")
        self.doc = repo.insert_document(
            self.conn, tenant_id="ta", owner_id="u", filename="a.pdf",
            pdf_object_key="k", sha256="0" * 64, ingestion_version="v1")
        repo.update_document_status(self.conn, self.doc, "ready", page_count=5)
        # 同一图片资产在不同页的两个 occurrence(复用同一对象键即可)
        self.asset = repo.get_or_create_asset(
            self.conn, tenant_id="ta", sha256="1" * 64, object_key="img/1",
            width=100, height=80, mime_type="image/png")
        self.occ_p2 = _make_occ(self.conn, self.doc, "ta", self.asset, 2, "1.1")
        self.occ_p5 = _make_occ(self.conn, self.doc, "ta", self.asset, 5, "1.5")
        # 另一个租户的图(越权引用目标)
        foreign_doc = repo.insert_document(
            self.conn, tenant_id="tb", owner_id="u", filename="b.pdf",
            pdf_object_key="k2", sha256="2" * 64, ingestion_version="v1")
        foreign_asset = repo.get_or_create_asset(
            self.conn, tenant_id="tb", sha256="3" * 64, object_key="img/3",
            width=10, height=10, mime_type="image/png")
        self.occ_foreign = _make_occ(self.conn, foreign_doc, "tb", foreign_asset, 1, "9.9")
        # 一个正文块(供 chunk 引用)
        self.chunk = repo.insert_chunk(
            self.conn, tenant_id="ta", document_id=self.doc, ingestion_version="v1",
            section=None, text="内容", page_start=3, page_end=3, paragraph_ids=["p1"],
            referenced_image_ids=[], nearby_image_ids=[])
        self.knowledge = KnowledgeService(store=None, index_service=None)
        # _assemble / answer_stream 只用到 settings.db_path;模型在一致性用例里替换为假 agent
        self.qa = QAService(SimpleNamespace(db_path=self.db_path), self.knowledge)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _assemble(self, answer_text, question="问题", chunks=(), images=()):
        tracker = {"chunks": set(chunks), "images": set(images)}
        return self.qa._assemble(self.conn, self.ctx, answer_text, question, tracker, set(), set())

    def test_same_asset_two_occurrences_both_kept_with_own_pages(self):
        # 显式引用路径不做资产去重:图 1.1(p2)与图 1.5(p5)同资产,各自保留出处页
        result = self._assemble(
            f"先看。\n\n[image:{self.occ_p2}]\n\n再讲。\n\n[image:{self.occ_p5}]",
            images=(self.occ_p2, self.occ_p5))
        entries = result["images"]
        self.assertEqual([e["image_occurrence_id"] for e in entries], [self.occ_p2, self.occ_p5])
        self.assertEqual([e["page"] for e in entries], [2, 5])     # 各自页码,不互相顶替
        self.assertEqual([e["figure_number"] for e in entries], ["1.1", "1.5"])

    def test_fabricated_missing_and_foreign_ids_produce_no_entries(self):
        result = self._assemble(
            f"a[image:occ_fabricated] b[image:{self.occ_foreign}] c",
            images=(self.occ_foreign,))    # 即使 tracker 里混入越权 ID,归属校验仍拦截
        self.assertEqual(result["images"], [])
        self.assertEqual(result["citations"], [])

    def test_marker_order_drives_entry_order_and_dedupes_repeat(self):
        result = self._assemble(
            f"[image:{self.occ_p5}] 中段 [image:{self.occ_p2}] 再提 [image:{self.occ_p5}]",
            images=(self.occ_p2, self.occ_p5))
        self.assertEqual([e["image_occurrence_id"] for e in result["images"]],
                         [self.occ_p5, self.occ_p2])                # 按正文首次出现排序,重复不重复

    def test_fallback_candidates_when_user_asks_without_anchor(self):
        result = self._assemble("这段没有图片锚点", question="把相关图给我看",
                                images=(self.occ_p2,))
        self.assertEqual([e["image_occurrence_id"] for e in result["images"]], [self.occ_p2])

    def test_no_fallback_when_question_does_not_ask(self):
        result = self._assemble("纯文字回答", question="什么是光刻", images=(self.occ_p2,))
        self.assertEqual(result["images"], [])

    def test_chunk_citation_still_validated(self):
        result = self._assemble(f"见 [chunk:{self.chunk}]", chunks=(self.chunk,))
        self.assertEqual(result["citations"],
                         [{"document_id": self.doc, "chunk_id": self.chunk, "page": 3}])

    def test_answer_and_stream_done_consistent(self):
        answer_text = f"看图。\n\n[image:{self.occ_p2}]\n\n解释。"
        self.qa._build_agent = lambda tools: _FakeAgent(answer_text)   # 假模型,绕过 deepagents
        tracker_hook = {"chunks": set(), "images": {self.occ_p2}}
        self.qa._make_tools = lambda ctx: ([], tracker_hook)

        direct = self.qa.answer(self.conn, self.ctx, "讲一讲")
        events = list(self.qa.answer_stream(self.ctx, "讲一讲"))
        done = next(e for e in events if e["type"] == "done")
        for key in ("answer", "citations", "images"):
            self.assertEqual(direct[key], done[key], key)
        self.assertEqual([e["image_occurrence_id"] for e in done["images"]], [self.occ_p2])


class _FakeMessage:
    type = "ai"

    def __init__(self, content):
        self.content = content


class _FakeChunk:
    type = "AIMessageChunk"
    tool_call_chunks = None
    additional_kwargs = {}

    def __init__(self, content):
        self.content = content


class _FakeAgent:
    """固定回答的假 agent:invoke 返回终态消息,stream 逐段流出(含被拆开的图片标记)。"""

    def __init__(self, answer_text):
        self.answer_text = answer_text

    def invoke(self, _state):
        return {"messages": [_FakeMessage(self.answer_text)]}

    def stream(self, _state, stream_mode=None):
        mid = len(self.answer_text) // 2
        for piece in (self.answer_text[:mid], self.answer_text[mid:]):
            yield _FakeChunk(piece), {}


if __name__ == "__main__":
    unittest.main()
