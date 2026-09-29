"""位置文案:服务端统一计算,前端只显示(DEV_multi_format_documents.md §3.2/§3.9)。

locator_kind → page_number 的语义与展示:
- page:    真实页码   -> "p.7" / "p.1–2"
- slide:   幻灯片序号 -> "幻灯片 3"
- sheet:   工作表序号 -> "工作表「名」"(名取 section;无名回退序号)
- section: 虚拟分段号 -> "章节路径 · 第 N 段";无章节 -> "第 N 段"(不再退化为一律"全文")
"""

from __future__ import annotations


def locator_label(kind: str, page_start: int | None, page_end: int | None = None,
                  section: str | None = None) -> str:
    if kind == "slide":
        return f"幻灯片 {page_start}"
    if kind == "sheet":
        return f"工作表「{section}」" if section else f"工作表 {page_start}"
    if kind == "section":
        seg = f"第 {page_start} 段" if page_start else ""
        return " · ".join(p for p in (section, seg) if p) or "全文"
    # page(默认)
    if page_end and page_start and page_end != page_start:
        return f"p.{page_start}–{page_end}"
    return f"p.{page_start}"
