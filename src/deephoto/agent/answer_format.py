"""回答正文规整:让 [image:ID] 图片锚点满足排版契约(纯函数,无 IO)。

契约:
- 每个锚点独占一段(前后各空一行),不出现在句子中间;
- 同一个 ID 只保留第一次出现,后续重复的锚点整体删除;
- 围栏代码块与行内代码里的示例标记原样保留,不当作锚点。

模型经常不遵守提示词里的排版要求(把标记写在句中、重复插同一张图),
所以由后端在校验组装时统一规整,前端与历史消息拿到的都是规整后的正文。
"""

from __future__ import annotations

import re

_INLINE_CODE = re.compile(r"`[^`\n]*`")
_ANCHOR = re.compile(r"\[image:([A-Za-z0-9_]+)\]")


def _split_code(line: str) -> list[tuple[bool, str]]:
    """把一行切成 (是否行内代码, 文本) 片段。"""
    parts: list[tuple[bool, str]] = []
    last = 0
    for m in _INLINE_CODE.finditer(line):
        if m.start() > last:
            parts.append((False, line[last:m.start()]))
        parts.append((True, m.group(0)))
        last = m.end()
    if last < len(line):
        parts.append((False, line[last:]))
    return parts


def normalize_image_anchors(text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []          # 输出行;空串表示空行
    in_fence = False

    def add_blank() -> None:
        if out and out[-1] != "":
            out.append("")

    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue

        anchors: list[str] = []
        pieces: list[str] = []
        for is_code, part in _split_code(line):
            if is_code:
                pieces.append(part)
                continue
            for m in _ANCHOR.finditer(part):
                if m.group(1) not in seen:
                    seen.add(m.group(1))
                    anchors.append(m.group(1))
            pieces.append(_ANCHOR.sub("", part))
        rest = "".join(pieces)

        if not anchors:
            if rest.strip() == "" and _ANCHOR.search(line):
                continue                      # 整行只剩被删除的重复锚点:不留空洞
            if rest.strip() == "":
                add_blank()                   # 连续空行折叠为一个
            else:
                out.append(rest)
            continue

        if rest.strip():
            out.append(rest.rstrip())
        for anchor in anchors:
            add_blank()
            out.append(f"[image:{anchor}]")
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip("\n")
