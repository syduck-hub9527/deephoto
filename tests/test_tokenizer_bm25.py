import unittest

import _bootstrap  # noqa: F401
from deephoto.indexing.bm25 import BM25Index, normalize
from deephoto.indexing.tokenizer import tokenize


class TokenizerTest(unittest.TestCase):
    def test_cjk_bigrams(self):
        tokens = tokenize("注意力机制")
        self.assertIn("注意", tokens)
        self.assertIn("力机", tokens)
        self.assertIn("机制", tokens)

    def test_mixed(self):
        tokens = tokenize("使用 Transformer 结构,见图 3")
        self.assertIn("transformer", tokens)
        self.assertIn("3", tokens)

    def test_lowercase(self):
        self.assertIn("attention", tokenize("Attention"))


class BM25Test(unittest.TestCase):
    def test_ranking(self):
        index = BM25Index()
        index.build({
            "a": "注意力机制通过 Query 与 Key 计算相关性",
            "b": "卷积网络用于图像分类任务",
            "c": "图 3 展示了注意力机制的结构",
        })
        scores = index.scores("注意力机制")
        self.assertGreater(scores.get("a", 0), 0)
        self.assertGreater(scores.get("c", 0), 0)
        self.assertEqual(scores.get("b", 0), 0)

    def test_term_precision(self):
        index = BM25Index()
        index.build({"a": "实验结果见正文", "b": "图 3 结构示意图"})
        scores = index.scores("图 3")
        self.assertGreater(scores.get("b", 0), scores.get("a", 0))

    def test_empty(self):
        index = BM25Index()
        self.assertEqual(index.scores("query"), {})

    def test_normalize(self):
        self.assertEqual(normalize({}), {})
        self.assertEqual(normalize({"a": 4.0, "b": 2.0}), {"a": 1.0, "b": 0.5})
        self.assertEqual(normalize({"a": 0.0}), {"a": 0.0})


if __name__ == "__main__":
    unittest.main()
