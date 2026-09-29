"""错误信息脱敏:写入数据库(uncertain_details / documents.error)或日志前统一处理。

原则:保留"错误类别 + HTTP 状态码 + 必要的短摘要",去掉密钥、Authorization、
data URL / base64 正文;摘要限长,避免把整段服务端响应正文落库。
"""

from __future__ import annotations

import re
from typing import Iterable

_MAX_SUMMARY_CHARS = 200

_PATTERNS = [
    # data URL(整张图片的 base64)
    (re.compile(r"data:[\w/+.\-]+;base64,[A-Za-z0-9+/=_\-]+"), "[data-url]"),
    # Authorization / Bearer
    (re.compile(r"(?i)bearer\s+[^\s\"',;}]+"), "Bearer [redacted]"),
    # 常见 sk- 风格密钥(含被服务端部分遮蔽的 sk-abc****xyz)
    (re.compile(r"\bsk-[A-Za-z0-9_\-*]{4,}"), "[redacted-key]"),
    # api_key=... / "api-key": "..." / apikey: ...
    (re.compile(r"(?i)(api[_\-]?key|authorization|token)([\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+"),
     r"\1\2[redacted]"),
    # 其余超长 base64 样式连续串
    (re.compile(r"[A-Za-z0-9+/=]{200,}"), "[blob]"),
]


def redact_secrets(text: str, secrets: Iterable[str | None] = ()) -> str:
    """先按"已配置的密钥原值"精确替换,再按常见模式替换。"""
    out = text
    for secret in secrets:
        if secret and len(secret.strip()) >= 4:
            out = out.replace(secret.strip(), "[redacted]")
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def error_summary(exc: BaseException, secrets: Iterable[str | None] = (),
                  *, max_chars: int = _MAX_SUMMARY_CHARS) -> str:
    """`ClassName(status=NNN): 脱敏后的短摘要`;无状态码则省略括号部分。"""
    status = getattr(exc, "status_code", None)
    head = type(exc).__name__ + (f"(status={status})" if isinstance(status, int) else "")
    message = redact_secrets(str(exc), secrets).strip().replace("\n", " ")
    if len(message) > max_chars:
        message = message[:max_chars] + "…"
    return f"{head}: {message}" if message else head
