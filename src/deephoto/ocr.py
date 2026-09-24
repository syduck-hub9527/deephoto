"""OCR 模型配置与 OpenAI 兼容 OCR 服务适配器。

本地部署和第三方平台都通过同一个 ``/chat/completions`` 协议接入。这样
服务端不需要绑定某一家 SDK,也不会把第三方密钥放进前端请求。适配器只负责
传入页面图片并提取文本,版面解析和入库仍由 pipeline 负责。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

OCR_PROVIDER_DISABLED = "disabled"
OCR_PROVIDER_LOCAL = "local"
OCR_PROVIDER_THIRD_PARTY = "third_party"
OCR_PROVIDER_MINERU = "mineru"

DEFAULT_LOCAL_OCR_URL = "http://127.0.0.1:8001/v1"
DEFAULT_OCR_TIMEOUT_SECONDS = 60.0
SUPPORTED_OCR_PROVIDERS = (
    {
        "id": OCR_PROVIDER_MINERU,
        "name": "MineU 整PDF解析",
        "description": "整份 PDF 交给 MineU 云端异步解析,失败时降级为按页 OCR",
    },
    {
        "id": OCR_PROVIDER_LOCAL,
        "name": "本地部署",
        "description": "连接本机或内网的 OpenAI 兼容 OCR 服务(按页)",
    },
    {
        "id": OCR_PROVIDER_THIRD_PARTY,
        "name": "第三方平台",
        "description": "连接云厂商提供的 OpenAI 兼容 OCR 服务(按页)",
    },
)


class OCRConfigurationError(ValueError):
    """OCR 配置不完整或不受支持。"""


class OCRServiceError(RuntimeError):
    """OCR 服务调用失败或响应格式不正确。"""


@dataclass(frozen=True)
class OCRConfig:
    provider: str = OCR_PROVIDER_DISABLED
    model: str = ""
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return self.provider != OCR_PROVIDER_DISABLED

    def normalized(self) -> "OCRConfig":
        provider = normalize_provider(self.provider)
        base_url = self.base_url.strip() if self.base_url else None
        model = self.model.strip()
        api_key = self.api_key.strip() if self.api_key else None
        if provider == OCR_PROVIDER_LOCAL and not base_url:
            base_url = DEFAULT_LOCAL_OCR_URL
        config = OCRConfig(provider, model, base_url, api_key, float(self.timeout_seconds))
        config.validate()
        return config

    def validate(self) -> None:
        if self.provider == OCR_PROVIDER_DISABLED:
            return
        if self.provider not in {OCR_PROVIDER_LOCAL, OCR_PROVIDER_THIRD_PARTY, OCR_PROVIDER_MINERU}:
            raise OCRConfigurationError("不支持的 OCR 服务类型")
        if self.provider == OCR_PROVIDER_MINERU:
            # MineU 只需要 Token;base_url/model 不是必填
            if not self.api_key:
                raise OCRConfigurationError("请填写 MineU API Token")
            if self.base_url:
                parsed = urlparse(self.base_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise OCRConfigurationError("MineU 服务地址必须是 http 或 https URL")
            return
        if not self.model:
            raise OCRConfigurationError("请选择或填写 OCR 模型名称")
        if not self.base_url:
            raise OCRConfigurationError("请填写 OCR 服务地址")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise OCRConfigurationError("OCR 服务地址必须是 http 或 https URL")
        if parsed.username or parsed.password:
            raise OCRConfigurationError("OCR 服务地址不能包含用户名或密码")
        if not 1 <= float(self.timeout_seconds) <= 300:
            raise OCRConfigurationError("OCR 超时时间必须在 1 到 300 秒之间")


@dataclass(frozen=True)
class OCRResult:
    text: str
    raw: dict[str, Any]


class OpenAICompatibleOCRProvider:
    """调用 OpenAI 兼容多模态接口的 OCR 提供商。"""

    def __init__(self, config: OCRConfig,
                 opener: Callable[..., Any] | None = None):
        self.config = config.normalized()
        self._opener = opener or urlopen

    def recognize(self, image_bytes: bytes, mime_type: str = "image/png") -> OCRResult:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.config.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "请识别图片中的全部文字，按阅读顺序输出。只输出 OCR 文本。"},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime_type};base64,{encoded}",
                    }},
                ],
            }],
            "temperature": 0,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = Request(_completion_url(self.config.base_url or ""), data=body,
                          headers=headers, method="POST")
        try:
            response = self._opener(request, timeout=self.config.timeout_seconds)
            response_body = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise OCRServiceError(f"OCR 服务返回 HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise OCRServiceError(f"无法连接 OCR 服务: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OCRServiceError("OCR 服务请求超时") from exc
        try:
            data = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OCRServiceError("OCR 服务返回的不是有效 JSON") from exc
        return OCRResult(text=_extract_text(data), raw=data)


class LocalOCRProvider(OpenAICompatibleOCRProvider):
    """本地部署 OCR 的语义别名,便于调用方按来源区分。"""


class ThirdPartyOCRProvider(OpenAICompatibleOCRProvider):
    """第三方平台 OCR 的语义别名。"""


def build_ocr_provider(config: OCRConfig) -> OpenAICompatibleOCRProvider | None:
    normalized = config.normalized()
    if not normalized.enabled:
        return None
    if normalized.provider == OCR_PROVIDER_LOCAL:
        return LocalOCRProvider(normalized)
    if normalized.provider == OCR_PROVIDER_THIRD_PARTY:
        return ThirdPartyOCRProvider(normalized)
    raise OCRConfigurationError("不支持的 OCR 服务类型")


def normalize_provider(value: str | None) -> str:
    """兼容旧的 ``remote``/``openai_compatible`` 写法。"""
    value = (value or OCR_PROVIDER_DISABLED).strip().lower()
    if value in {"remote", "openai", "openai_compatible", "third-party", "thirdparty"}:
        return OCR_PROVIDER_THIRD_PARTY
    if value in {"miner-u", "mineru", "mineu"}:
        return OCR_PROVIDER_MINERU
    if value in {"off", "none", ""}:
        return OCR_PROVIDER_DISABLED
    return value


def _completion_url(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def _extract_text(payload: dict[str, Any]) -> str:
    """提取常见 OpenAI、DashScope 兼容响应中的文本。"""
    candidates: list[Any] = []
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message") or first.get("output") or first
            if isinstance(message, dict):
                candidates.append(message.get("content"))
                candidates.append(message.get("text"))
    output = payload.get("output")
    if isinstance(output, dict):
        out_choices = output.get("choices")
        if isinstance(out_choices, list) and out_choices:
            first = out_choices[0]
            if isinstance(first, dict):
                message = first.get("message") or first
                if isinstance(message, dict):
                    candidates.append(message.get("content"))
                    candidates.append(message.get("text"))
        candidates.extend([output.get("text"), output.get("content")])
    candidates.extend([payload.get("text"), payload.get("content")])
    for candidate in candidates:
        text = _content_to_text(candidate).strip()
        if text:
            return text
    raise OCRServiceError("OCR 服务返回为空或缺少可识别文本")


def _content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_to_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                return _content_to_text(value[key])
    return ""
