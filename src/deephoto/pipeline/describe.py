"""用图片描述模型(DESCRIPTION_* 配置,默认 qwen3.8-omni-flash)为图片生成检索描述(开发文档§3.4)。

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
    *,
    retry_hint: str | None = None,
) -> dict:
    """返回 {visible_summary, ..., uncertain_details, diagnostic}。

    diagnostic["result"]: "ok" 调用完成且 JSON 解析成功;"parse_failed" 调用完成
    但输出不是有效 JSON(内容降级为空描述,不能按成功计数)。观测据此区分
    正常描述与格式失败;不根据描述是否为空猜结果(空也可能是合法的"不清楚")。
    parse_failed 时 diagnostic 另含机读字段 reason/code(失败分类)与
    truncated(截断信号),供集中重试决定 max_tokens 与提示词(§4.5);
    ok 返回不含这些键,保持原有结构不变。
    retry_hint:重试时追加在提示词与上下文之后的格式提醒(不改动 _PROMPT 本体)。
    """
    from langchain_core.messages import HumanMessage

    meta_lines = []
    if section:
        meta_lines.append(f"所属章节:{section}")
    if caption:
        meta_lines.append(f"原文图注:{caption}")
    if context_text:
        meta_lines.append(f"相关正文摘录:{context_text[:800]}")
    meta = "\n".join(meta_lines) or "(无图注与正文上下文)"

    text = _PROMPT + "\n\n" + meta
    if retry_hint:
        text += "\n\n" + retry_hint
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"
    message = HumanMessage(content=[
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": data_url}},
    ])
    response = chat_model.invoke([message])
    raw = _response_text(response.content)
    result = parse_description_json(raw)
    if result["diagnostic"]["result"] != "ok":
        result["diagnostic"]["truncated"] = _truncated(response, raw)
    return result


def _truncated(response, raw: str) -> bool:
    """截断信号:优先响应元数据 finish_reason == "length";取不到时用启发式
    (原始文本中 { 多于 })。是否截断决定重试是否升级 max_tokens(§4.4)。"""
    metadata = getattr(response, "response_metadata", None) or {}
    finish_reason = metadata.get("finish_reason")
    if finish_reason is not None:
        return finish_reason == "length"
    return raw.count("{") > raw.count("}")


def _response_text(content) -> str:
    """提取响应文本:字符串直接用;内容块列表只取 text 类型,不 str(list) 后当 JSON。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def parse_description_json(raw: str) -> dict:
    """宽松解析模型输出:提取第一个 JSON 对象并校验字段类型。

    空输出、截断、非法 JSON、非对象根值、字段类型不匹配一律 parse_failed,
    不能当成功(空字符串字段值本身可能是合法的"不清楚",不按内容判)。
    失败分支的 diagnostic["code"] 机读分类:empty / no_json / invalid_json /
    not_object / field_type,供重试选择 max_tokens 与提示策略(§4.4)。
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        if not raw.strip():
            return _fallback("模型未返回内容(空输出)", "empty")
        return _fallback("模型未返回 JSON 对象", "no_json")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return _fallback(f"JSON 解析失败:{type(exc).__name__}", "invalid_json")
    if not isinstance(data, dict):
        return _fallback("模型返回的 JSON 不是对象", "not_object")
    for key in ("visible_summary", "caption", "context_summary"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return _fallback(f"{key} 字段不是字符串", "field_type")
    for key in ("visible_labels", "uncertain_details"):
        if data.get(key) is not None and not isinstance(data[key], list):
            return _fallback(f"{key} 字段不是列表", "field_type")
    return {
        "visible_summary": str(data.get("visible_summary") or ""),
        "visible_labels": [str(x) for x in data.get("visible_labels") or []][:20],
        "caption": str(data.get("caption") or ""),
        "context_summary": str(data.get("context_summary") or ""),
        "uncertain_details": [str(x) for x in data.get("uncertain_details") or []][:20],
        "diagnostic": {"result": "ok"},
    }


def _fallback(reason: str, code: str) -> dict:
    return {
        "visible_summary": "",
        "visible_labels": [],
        "caption": "",
        "context_summary": "",
        "uncertain_details": [reason],
        "diagnostic": {"result": "parse_failed", "reason": reason, "code": code},
    }
