"""用 Kimi K3 为图片生成检索描述(开发文档§3.4)。

输入完整裁图(base64)、图注、所属章节与相关正文,输出结构化 JSON。
visible_summary 必须依据图像;看不清的细节不得猜测,记入 uncertain_details。
描述记录模型与提示词版本,支持将来重新生成。
"""

from __future__ import annotations

import base64
import json
import re

DESC_PROMPT_VERSION = "v1"

_SCHEMA_EXAMPLE = {
    "visible_summary": "图中实际可见的结构或过程",
    "visible_labels": ["图内标签 A", "图内标签 B"],
    "caption": "原文图注",
    "context_summary": "正文如何解释这张图",
    "uncertain_details": ["看不清的细节"],
}

_PROMPT = (
    "你正在为一篇文档的配图建立检索描述。请仔细观察这张图片,并结合给出的图注与正文,"
    "只输出一个 JSON 对象,字段如下:\n"
    f"{json.dumps(_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}\n"
    "要求:\n"
    "1. visible_summary 只描述图像中实际可见的内容(结构、流程、坐标轴、曲线趋势等),不得猜测;\n"
    "2. visible_labels 列出图内可读出的文字标签;\n"
    "3. caption 照抄原文图注,没有则为空字符串;\n"
    "4. context_summary 概括所给正文如何解释这张图,并注明来自正文;\n"
    "5. 看不清的数字、箭头、坐标轴、符号,列入 uncertain_details,不要编造;\n"
    "6. 只输出 JSON,不要输出其他内容。"
)


def describe_image(
    chat_model,
    image_bytes: bytes,
    mime_type: str,
    caption: str | None,
    section: str | None,
    context_text: str | None,
) -> dict:
    """返回 {visible_summary, visible_labels, caption, context_summary, uncertain_details}。"""
    from langchain_core.messages import HumanMessage

    meta_lines = []
    if section:
        meta_lines.append(f"所属章节:{section}")
    if caption:
        meta_lines.append(f"原文图注:{caption}")
    if context_text:
        meta_lines.append(f"相关正文摘录:{context_text[:800]}")
    meta = "\n".join(meta_lines) or "(无图注与正文上下文)"

    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"
    message = HumanMessage(content=[
        {"type": "text", "text": _PROMPT + "\n\n" + meta},
        {"type": "image_url", "image_url": {"url": data_url}},
    ])
    response = chat_model.invoke([message])
    return parse_description_json(response.content if isinstance(response.content, str) else str(response.content))


def parse_description_json(raw: str) -> dict:
    """宽松解析模型输出:提取第一个 JSON 对象并校验字段。"""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return _fallback(f"模型未返回 JSON:{raw[:200]}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return _fallback(f"JSON 解析失败:{exc}")
    return {
        "visible_summary": str(data.get("visible_summary") or ""),
        "visible_labels": [str(x) for x in data.get("visible_labels") or []][:20],
        "caption": str(data.get("caption") or ""),
        "context_summary": str(data.get("context_summary") or ""),
        "uncertain_details": [str(x) for x in data.get("uncertain_details") or []][:20],
    }


def _fallback(reason: str) -> dict:
    return {
        "visible_summary": "",
        "visible_labels": [],
        "caption": "",
        "context_summary": "",
        "uncertain_details": [reason],
    }
