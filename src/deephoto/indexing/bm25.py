"""BM25 关键词检索(纯标准库,Okapi 变体,k1=1.5, b=0.75)。

用于准确命中“图 3”、专有术语与公式名称(开发文档§3.5)。
"""

from __future__ import annotations

import math
from collections import Counter

from .tokenizer import tokenize

K1 = 1.5
B = 0.75


class BM25Index:
    def __init__(self) -> None:
        self.doc_tokens: dict[str, list[str]] = {}
        self.doc_freq: Counter[str] = Counter()
        self.avg_len = 0.0

    def build(self, documents: dict[str, str]) -> None:
        """documents: {doc_id: text}。整体重建,适合首版单文档级规模。"""
        self.doc_tokens = {}
        self.doc_freq = Counter()
        total_len = 0
        for doc_id, text in documents.items():
            tokens = tokenize(text)
            self.doc_tokens[doc_id] = tokens
            total_len += len(tokens)
            for token in set(tokens):
                self.doc_freq[token] += 1
        self.avg_len = total_len / len(self.doc_tokens) if self.doc_tokens else 0.0

    def scores(self, query: str) -> dict[str, float]:
        if not self.doc_tokens:
            return {}
        n_docs = len(self.doc_tokens)
        result: dict[str, float] = {}
        for token in set(tokenize(query)):
            df = self.doc_freq.get(token, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for doc_id, tokens in self.doc_tokens.items():
                tf = tokens.count(token)
                if tf == 0:
                    continue
                norm = 1 - B + B * len(tokens) / (self.avg_len or 1)
                result[doc_id] = result.get(doc_id, 0.0) + idf * tf * (K1 + 1) / (tf + K1 * norm)
        return result


def normalize(scores: dict[str, float]) -> dict[str, float]:
    """按最大值归一到 [0,1];空集返回空。"""
    if not scores:
        return {}
    peak = max(scores.values())
    if peak <= 0:
        return {k: 0.0 for k in scores}
    return {k: v / peak for k, v in scores.items()}
