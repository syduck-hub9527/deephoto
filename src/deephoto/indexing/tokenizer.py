"""中英文混合分词(纯标准库)。

- 连续的 ASCII 字母/数字作为一个词(小写化);
- 连续的 CJK 字符取滑窗二元组(bigram),单字保留为单词;
  例:“注意力机制” → 注意, 意力, 力机, 机制
够用的首版方案;后续可换 jieba 等分词器而不影响上层。
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")
_WORD_RE = re.compile(r"[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    # 交替切出 CJK 段与非 CJK 段
    for match in re.finditer(r"[一-鿿㐀-䶿]+|[^一-鿿㐀-䶿]+", text):
        segment = match.group(0)
        if _CJK_RE.fullmatch(segment):
            if len(segment) == 1:
                tokens.append(segment)
            else:
                tokens.extend(segment[i:i + 2] for i in range(len(segment) - 1))
                tokens.append(segment[0])   # 保留首字单词,召回单字图号类查询
        else:
            tokens.extend(w.lower() for w in _WORD_RE.findall(segment))
    return tokens
