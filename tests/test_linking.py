import unittest

import _bootstrap  # noqa: F401
from deephoto.pipeline.linking import CONF_DISPLAY_THRESHOLD, resolve_links


class LinkingTest(unittest.TestCase):
    def setUp(self):
        self.occurrences = [
            {"id": "occ_a", "page_number": 4, "figure_number": "3", "caption": "图 3:注意力机制结构图"},
            {"id": "occ_b", "page_number": 7, "figure_number": None, "caption": None},
        ]

    def test_reference_link(self):
        chunks = [{"id": "chk_1", "text": "如图 3 所示……", "page_start": 5, "page_end": 5,
                   "referenced_image_ids": ["occ_a"]}]
        links = resolve_links(chunks, self.occurrences)
        ref = [l for l in links if l.relation == "references"]
        self.assertEqual(len(ref), 1)
        self.assertEqual(ref[0].image_occurrence_id, "occ_a")
        self.assertGreaterEqual(ref[0].confidence, CONF_DISPLAY_THRESHOLD)

    def test_caption_of_link(self):
        chunks = [{"id": "chk_1", "text": "……\n图 3:注意力机制结构图\n……", "page_start": 4,
                   "page_end": 4, "referenced_image_ids": []}]
        links = resolve_links(chunks, self.occurrences)
        cap = [l for l in links if l.relation == "caption_of"]
        self.assertEqual(len(cap), 1)
        self.assertEqual(cap[0].image_occurrence_id, "occ_a")

    def test_nearby_is_low_confidence(self):
        chunks = [{"id": "chk_1", "text": "无关正文。" * 50, "page_start": 7, "page_end": 7,
                   "referenced_image_ids": []}]
        links = resolve_links(chunks, self.occurrences)
        nearby = [l for l in links if l.relation == "nearby"]
        self.assertEqual(len(nearby), 1)
        self.assertEqual(nearby[0].image_occurrence_id, "occ_b")
        self.assertLess(nearby[0].confidence, CONF_DISPLAY_THRESHOLD)

    def test_no_duplicate_links(self):
        chunks = [{"id": "chk_1", "text": "图 3:注意力机制结构图 如图 3 所示", "page_start": 4,
                   "page_end": 4, "referenced_image_ids": ["occ_a"]}]
        links = resolve_links(chunks, self.occurrences)
        keys = [(l.chunk_id, l.image_occurrence_id, l.relation) for l in links]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
