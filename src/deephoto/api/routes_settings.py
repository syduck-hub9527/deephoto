"""设置 API。

设置按租户保存,这样同一租户的文档后台任务和前端选择使用同一份配置。
敏感的 OCR API Key 只写入服务端数据库,响应中只返回是否已配置。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import repo
from ..ocr import (
    OCRConfig,
    OCRConfigurationError,
    SUPPORTED_OCR_PROVIDERS,
    normalize_provider,
)
from ..security import AuthContext
from .deps import CtxDep, conn_for

router = APIRouter(prefix="/api/settings", tags=["settings"])

_SECTIONS = [
    {"id": "ocr", "label": "OCR 配置", "description": "扫描页文字识别服务"},
]


class OCRSettingsUpdate(BaseModel):
    provider: str = Field(default="third_party", max_length=40)
    model: str = Field(default="", max_length=200)
    base_url: str | None = Field(default=None, max_length=1000)
    # 留空表示保留已有密钥;需要删除时使用 clear_api_key。
    api_key: str | None = Field(default=None, max_length=1000)
    clear_api_key: bool = False
    timeout_seconds: float = Field(default=60, ge=1, le=300)


def _current(request: Request, tenant_id: str) -> dict[str, object]:
    return repo.get_ocr_settings(
        conn_for(request), tenant_id, request.app.state.settings.ocr_defaults,
    )


def _public_ocr(config: dict[str, object]) -> dict[str, object]:
    api_key = str(config.get("api_key") or "")
    return {
        "provider": normalize_provider(str(config.get("provider") or "disabled")),
        "model": config.get("model", ""),
        "base_url": config.get("base_url"),
        "timeout_seconds": config.get("timeout_seconds", 60.0),
        "api_key_configured": bool(api_key),
        "api_key_masked": "••••••" if api_key else "",
    }


@router.get("")
def get_settings(request: Request, ctx: AuthContext = CtxDep):
    config = _current(request, ctx.tenant_id)
    return {
        "sections": _SECTIONS,
        "ocr": _public_ocr(config),
        "ocr_providers": list(SUPPORTED_OCR_PROVIDERS),
    }


@router.put("/ocr")
def update_ocr_settings(request: Request, body: OCRSettingsUpdate,
                        ctx: AuthContext = CtxDep):
    current = _current(request, ctx.tenant_id)
    provider = normalize_provider(body.provider)
    api_key = None if body.clear_api_key else current.get("api_key")
    if body.api_key and body.api_key.strip():
        api_key = body.api_key.strip()
    try:
        config = OCRConfig(
            provider=provider,
            model=body.model.strip(),
            base_url=body.base_url.strip() if body.base_url else None,
            api_key=str(api_key) if api_key else None,
            timeout_seconds=body.timeout_seconds,
        ).normalized()
    except (OCRConfigurationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    repo.save_ocr_settings(
        conn_for(request), ctx.tenant_id,
        provider=config.provider, model=config.model, base_url=config.base_url,
        api_key=config.api_key, timeout_seconds=config.timeout_seconds,
    )
    return {"ocr": _public_ocr({
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "timeout_seconds": config.timeout_seconds,
    })}
