import unittest

import _bootstrap  # noqa: F401
from deephoto.indexing.keys import stable_item_id


class StableKeyTest(unittest.TestCase):
    def test_deterministic(self):
        a = stable_item_id("t1", "doc1", "v1", "chunk", "chk_1")
        b = stable_item_id("t1", "doc1", "v1", "chunk", "chk_1")
        self.assertEqual(a, b)

    def test_distinguishes_inputs(self):
        base = stable_item_id("t1", "doc1", "v1", "chunk", "chk_1")
        self.assertNotEqual(base, stable_item_id("t2", "doc1", "v1", "chunk", "chk_1"))
        self.assertNotEqual(base, stable_item_id("t1", "doc2", "v1", "chunk", "chk_1"))
        self.assertNotEqual(base, stable_item_id("t1", "doc1", "v2", "chunk", "chk_1"))
        self.assertNotEqual(base, stable_item_id("t1", "doc1", "v1", "image", "chk_1"))
        self.assertNotEqual(base, stable_item_id("t1", "doc1", "v1", "chunk", "chk_2"))


if __name__ == "__main__":
    unittest.main()
