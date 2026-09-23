import time
import unittest

import _bootstrap  # noqa: F401
from deephoto.indexing.keys import stable_item_id
from deephoto.security import sign_resource, verify_resource_signature


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


class SignatureTest(unittest.TestCase):
    def test_roundtrip(self):
        expires, sig = sign_resource("secret", "doc_1", "images/occ_1", ttl_seconds=60)
        self.assertTrue(verify_resource_signature("secret", "doc_1", "images/occ_1", expires, sig))

    def test_wrong_resource(self):
        expires, sig = sign_resource("secret", "doc_1", "images/occ_1", ttl_seconds=60)
        self.assertFalse(verify_resource_signature("secret", "doc_1", "images/occ_2", expires, sig))
        self.assertFalse(verify_resource_signature("secret", "doc_2", "images/occ_1", expires, sig))

    def test_wrong_secret(self):
        expires, sig = sign_resource("secret", "doc_1", "images/occ_1", ttl_seconds=60)
        self.assertFalse(verify_resource_signature("other", "doc_1", "images/occ_1", expires, sig))

    def test_expired(self):
        expires, sig = sign_resource("secret", "doc_1", "images/occ_1", ttl_seconds=-1)
        self.assertFalse(verify_resource_signature("secret", "doc_1", "images/occ_1", expires, sig))


if __name__ == "__main__":
    unittest.main()
