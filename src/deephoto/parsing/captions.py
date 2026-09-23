"""图号、图注与正文引用的识别规则(纯函数,无第三方依赖)。

覆盖中英文常见写法:
  图 3 / 图3 / 图 2.1 / Figure 12 / Fig. 4a / 表 1 / Table 2
正文引用:
  见图 3 / 如图 2.1 所示 / as shown in Fig. 4 / see Figure 12
"""

from __future__ import annotations

import re

# 图号本体:数字+可选小节号/字母后缀,如 3、2.1、4a
_NUM = r"(\d+(?:\.\d+)*[a-zA-Z]?)"

# 行首图注: “图 3：…”、“Fig. 4a …”、“Table 2 - …”
_CAPTION_RE = re.compile(
    r"^\s*(?:图|表|Fig\.?|Figure|Table)\s*" + _NUM + r"\s*[:：.．\-—]?\s*",
    re.IGNORECASE,
)

# 正文引用(需带引导词,避免把“如图”之外的普通编号误当引用)
_REF_RE = re.compile(
    r"(?:如?见|参见|见下?图|如|如图|见图|as\s+shown\s+in|see|shown\s+in)\s*"
    r"(?:图|表|Fig\.?|Figure|Table)\s*" + _NUM,
    re.IGNORECASE,
)


def match_caption(text: str) -> tuple[str | None, str]:
    """若文本块以图注开头,返回 (规范化图号, 完整图注文本);否则 (None, text)。"""
    m = _CAPTION_RE.match(text.strip())
    if not m:
        return None, text
    return m.group(1), text.strip()


def is_caption(text: str) -> bool:
    return _CAPTION_RE.match(text.strip()) is not None


def find_referenced_figures(text: str) -> list[str]:
    """从正文文本中提取显式引用的图号(去重、保持出现顺序)。"""
    seen: dict[str, None] = {}
    for m in _REF_RE.finditer(text):
        seen.setdefault(m.group(1))
    return list(seen)


def mentions_figure(text: str, figure_number: str) -> bool:
    """判断文本是否提及某个具体图号(用于答案引用校验)。"""
    if not figure_number:
        return False
    pattern = re.compile(r"(?:图|表|Fig\.?|Figure|Table)\s*" + re.escape(figure_number) + r"(?![\d.a-zA-Z])", re.IGNORECASE)
    return pattern.search(text) is not None


def asks_for_images(question: str) -> bool:
    """用户问题是否明确要求看图(决定后端是否主动附带图片候选)。"""
    return re.search(r"(图|图片|照片|示意|流程图|结构图|figure|diagram|image|picture|chart)", question, re.IGNORECASE) is not None
