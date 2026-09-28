"""入库集成:MinerU ZIP(含图/表/图表)-> 图片资产、图文关联、表格文本 -> 检索。

MinerU 网络调用用假客户端替换,其余(ZIP 解析、建文档、分块、关联、索引、检索)走真实代码。
"""

import io
import json
import tempfile
import unittest
import zipfile
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
    from deephoto.parsing.mineru import MinerUResult, _zip_extract
    from deephoto.pipeline.ingest import IngestService
    from deephoto.security import LOCAL_CTX
    from deephoto.storage import ObjectStore


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="PNG")
    return buf.getvalue()


ITEMS = [
    {"type": "text", "text": "第1章 微电子制造引论", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "1.1 集成度", "text_level": 2, "page_idx": 0},
    {"type": "text", "text": "如图1.1所示,存储器容量增大时特征尺寸不断缩小。", "page_idx": 0},
    {"type": "image", "img_path": "images/page_0_image_1.png", "image_caption": ["图1.1 存储器容量与最小特征尺寸"],
     "content": "256MB 0.35μm 1GB 0.25μm", "page_idx": 0, "bbox": [100, 300, 900, 700]},
    {"type": "text", "text": "1.2 掺杂", "text_level": 2, "page_idx": 1},
    {"type": "text", "text": "常用掺杂剂见表1.1。", "page_idx": 1},
    {"type": "table", "img_path": "images/page_1_table_2.png", "table_caption": ["表1.1 硅的掺杂剂"],
     "table_body": "<table><tr><td>类型</td><td>元素</td></tr><tr><td>N型</td><td>砷、磷、锑</td></tr>"
                   "<tr><td>P型</td><td>硼</td></tr></table>", "page_idx": 1},
    {"type": "chart", "img_path": "images/page_1_chart_3.png", "chart_caption": ["图1.2 线宽随年份变化"],
     "content": "年份 | 线宽\n1995 | 0.35\n2005 | 0.09", "page_idx": 1},
]
IMAGES = {
    "images/page_0_image_1.png": _png("red") if HAVE_DEPS else b"",
    "images/page_1_table_2.png": _png("green") if HAVE_DEPS else b"",
    "images/page_1_chart_3.png": _png("blue") if HAVE_DEPS else b"",
}


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("abc_content_list.json", json.dumps(ITEMS, ensure_ascii=False))
        for name, data in IMAGES.items():
            z.writestr(name, data)
    return buf.getvalue()


class _FakeClient:
    def __init__(self, **kwargs):
        pass

    def parse_pdf(self, pdf_bytes, filename="document.pdf"):
        page_texts, elements = _zip_extract(_zip_bytes(), expected_pages=2)
        return MinerUResult(page_texts=page_texts, raw={}, elements=elements)


@unittest.skipUnless(HAVE_DEPS, "需要 numpy 与 pillow")
class IngestFiguresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(
            moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
            embedding_base_url=None, embedding_api_key=None, embedding_model=None,
            data_dir=root, max_upload_mb=100, ingestion_version="v2", mineru_api_key="tok")
        init_db(self.settings.db_path)
        self.conn = connect(self.settings.db_path)
        self.store = ObjectStore(root / "objects")
        pdf_key, digest = self.store.put(b"%PDF-1.4 fake", "pdf", "application/pdf")
        self.doc_id = repo.insert_document(
            self.conn, tenant_id=LOCAL_CTX.tenant_id, owner_id=LOCAL_CTX.user_id, filename="t.pdf",
            pdf_object_key=pdf_key, sha256=digest, ingestion_version="v2")
        self.index = IndexService()
        service = IngestService(self.settings, self.store, parser=None, index_service=self.index)
        with mock.patch("deephoto.pipeline.ingest.MinerUClient", _FakeClient):
            service.ingest(self.doc_id)
        self.knowledge = KnowledgeService(self.store, self.index)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_document_ready_with_three_occurrences(self):
        doc = repo.get_document(self.conn, self.doc_id)
        self.assertEqual((doc["status"], doc["page_count"]), ("ready", 2))
        occs = repo.occurrences_for_document(self.conn, self.doc_id)
        self.assertEqual(sorted(o["figure_number"] for o in occs), ["1.1", "1.2", "表1.1"])
        for occ in occs:
            asset = repo.get_asset(self.conn, occ["image_asset_id"])
            self.assertEqual((asset["width"], asset["height"], asset["mime_type"]), (40, 30, "image/png"))
            self.assertTrue(self.store.get(asset["original_object_key"]).startswith(b"\x89PNG"))

    def test_table_text_is_indexed_and_retrievable(self):
        result = self.knowledge.search(self.conn, LOCAL_CTX, "N型 掺杂剂 有哪些")
        text = "\n".join(c["text"] for c in result["chunks"])
        self.assertIn("N型 | 砷、磷、锑", text)
        self.assertIn("表1.1 硅的掺杂剂", text)

    def test_figure_question_anchors_image_and_ocr_text(self):
        result = self.knowledge.search(self.conn, LOCAL_CTX, "图 1.1 中 256MB 对应的最小特征尺寸是多少?")
        figs = {i["figure_number"]: i for i in result["images"]}
        self.assertIn("1.1", figs)
        self.assertNotIn("表1.1", [k for k, v in figs.items() if v["relation"] == "explicit_figure"])
        text = "\n".join(c["text"] for c in result["chunks"])
        self.assertIn("256MB 0.35μm", text)          # 图内文字进入了可检索正文

    def test_table_reference_links_to_table_not_figure(self):
        occs = {o["figure_number"]: o["id"] for o in repo.occurrences_for_document(self.conn, self.doc_id)}
        chunks = repo.chunks_for_document(self.conn, self.doc_id)
        ref_chunk = next(c for c in chunks if "常用掺杂剂见表1.1" in c["text"])
        self.assertIn(occs["表1.1"], ref_chunk["referenced_image_ids"])
        self.assertNotIn(occs["1.1"], ref_chunk["referenced_image_ids"])

    def test_caption_links_are_high_confidence(self):
        occs = {o["figure_number"]: o["id"] for o in repo.occurrences_for_document(self.conn, self.doc_id)}
        rows = self.conn.execute(
            "SELECT image_occurrence_id, relation, confidence FROM chunk_image_links").fetchall()
        caption_of = {r["image_occurrence_id"] for r in rows if r["relation"] == "caption_of" and r["confidence"] >= 0.8}
        for number in ("1.1", "表1.1", "1.2"):
            self.assertIn(occs[number], caption_of, number)

    def test_sections_are_set_on_chunks(self):
        sections = {c["section"] for c in repo.chunks_for_document(self.conn, self.doc_id)}
        self.assertIn("第1章 微电子制造引论 > 1.1 集成度", sections)
        self.assertIn("第1章 微电子制造引论 > 1.2 掺杂", sections)

    def test_image_endpoint_data_and_inspect_blocks(self):
        occ = next(o for o in repo.occurrences_for_document(self.conn, self.doc_id) if o["figure_number"] == "1.1")
        blocks = self.knowledge.image_content_blocks(self.conn, LOCAL_CTX, occ["id"])
        self.assertEqual([b["type"] for b in blocks], ["text", "image"])
        self.assertEqual(blocks[1]["mime_type"], "image/png")


if __name__ == "__main__":
    unittest.main()
