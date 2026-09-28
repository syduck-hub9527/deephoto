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
    r"^\s*(图|表|Fig\.?|Figure|Table)\s*" + _NUM + r"\s*[:：.．\-—]?\s*",
    re.IGNORECASE,
)

# 正文引用(需带引导词,避免把“如图”之外的普通编号误当引用)
_REF_RE = re.compile(
    r"(?:如?见|参见|见下?图|如|如图|见图|as\s+shown\s+in|see|shown\s+in)\s*"
    r"(图|表|Fig\.?|Figure|Table)\s*" + _NUM,
    re.IGNORECASE,
)

# 用户问题里的图号提及:不要求引导词(“图 1.1 中 256MB 对应……”)
_MENTION_RE = re.compile(r"(图|表|Fig\.?|Figure|Table)\s*" + _NUM + r"(?![\d.a-zA-Z])", re.IGNORECASE)

TABLE_PREFIX = "表"   # 表格编号带此前缀存储,与图号分属不同编号空间(图 1.1 ≠ 表 1.1)


def normalize_number(prefix: str, number: str) -> str:
    """规范化编号:图/Fig/Figure -> 裸号 \"1.1\";表/Table -> \"表1.1\"。"""
    if prefix.lower() in ("表", "table"):
        return TABLE_PREFIX + number
    return number


def display_label(figure_number: str | None) -> str:
    """展示用标签:\"1.1\" -> \"图 1.1\";\"表1.1\" -> \"表 1.1\";空 -> \"图片\"。"""
    if not figure_number:
        return "图片"
    if figure_number.startswith(TABLE_PREFIX):
        return f"{TABLE_PREFIX} {figure_number[len(TABLE_PREFIX):]}"
    return f"图 {figure_number}"


def match_caption(text: str) -> tuple[str | None, str]:
    """若文本块以图注开头,返回 (规范化图号, 完整图注文本);否则 (None, text)。"""
    m = _CAPTION_RE.match(text.strip())
    if not m:
        return None, text
    return normalize_number(m.group(1), m.group(2)), text.strip()


def is_caption(text: str) -> bool:
    return _CAPTION_RE.match(text.strip()) is not None


def find_referenced_figures(text: str) -> list[str]:
    """从正文文本中提取显式引用的图号(去重、保持出现顺序)。"""
    seen: dict[str, None] = {}
    for m in _REF_RE.finditer(text):
        seen.setdefault(normalize_number(m.group(1), m.group(2)))
    return list(seen)


def find_figure_mentions(text: str) -> list[str]:
    """提取文本中提到的所有图号/表号(不要求引导词,用于用户问题的图号锚定)。"""
    seen: dict[str, None] = {}
    for m in _MENTION_RE.finditer(text):
        seen.setdefault(normalize_number(m.group(1), m.group(2)))
    return list(seen)


def mentions_figure(text: str, figure_number: str) -> bool:
    """判断文本是否提及某个具体图号(用于答案引用校验)。"""
    if not figure_number:
        return False
    if figure_number.startswith(TABLE_PREFIX):
        prefix, number = r"(?:表|Table)", figure_number[len(TABLE_PREFIX):]
    else:
        prefix, number = r"(?:图|Fig\.?|Figure)", figure_number
    pattern = re.compile(prefix + r"\s*" + re.escape(number) + r"(?![\d.a-zA-Z])", re.IGNORECASE)
    return pattern.search(text) is not None


def asks_for_images(question: str) -> bool:
    """用户问题是否明确要求看图(决定后端是否主动附带图片候选)。"""
    return re.search(r"(图|图片|照片|示意|流程图|结构图|figure|diagram|image|picture|chart)", question, re.IGNORECASE) is not None
