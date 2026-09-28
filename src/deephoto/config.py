"""运行配置:全部来自环境变量,前缀 DEEPHOTO_。

集中在一处读取,业务代码不直接碰 os.environ。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import overload

_ENV_PREFIX = "DEEPHOTO_"


@overload
def _get(name: str, default: str) -> str: ...
@overload
def _get(name: str, default: None = None) -> str | None: ...
def _get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(_ENV_PREFIX + name, default)


@dataclass(frozen=True)
class Settings:
    # Kimi K3(多模态,OpenAI 兼容接口)
    moonshot_api_key: str | None
    moonshot_base_url: str
    chat_model: str
    chat_temperature: float

    # 可选嵌入端点(OpenAI 兼容);未配置则退化为纯关键词检索
    embedding_base_url: str | None
    embedding_api_key: str | None
    embedding_model: str | None

    # 存储
    data_dir: Path
    max_upload_mb: int
    ingestion_version: str

    # MinerU 云端解析:唯一的 PDF 解析器,写死不可选;Token 仅由服务端环境变量提供
    mineru_api_key: str | None = None
    mineru_base_url: str | None = None
    # 调试:设置后把 MinerU 返回的原始结果 ZIP 存到该目录,用于核对真实字段格式
    mineru_dump_dir: str | None = None

    @property
    def embeddings_enabled(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_model)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "deephoto.db"

    @property
    def progress_db_path(self) -> Path:
        """入库观测库:与业务库分离,避免业务长事务挡住进度可见性。"""
        return self.data_dir / "progress.db"

    @property
    def object_dir(self) -> Path:
        return self.data_dir / "objects"


def load_settings() -> Settings:
    """从环境变量加载配置。支持 .env 文件(简单解析,不引入额外依赖)。"""
    _load_dotenv()
    return Settings(
        moonshot_api_key=_get("MOONSHOT_API_KEY"),
        moonshot_base_url=_get("MOONSHOT_BASE_URL", "https://api.kimi.com/coding/v1"),
        chat_model=_get("CHAT_MODEL", "k3"),
        chat_temperature=float(_get("CHAT_TEMPERATURE", "1")),
        embedding_base_url=_get("EMBEDDING_BASE_URL"),
        embedding_api_key=_get("EMBEDDING_API_KEY"),
        embedding_model=_get("EMBEDDING_MODEL"),
        data_dir=Path(_get("DATA_DIR", "./data")).resolve(),
        max_upload_mb=int(_get("MAX_UPLOAD_MB", "100")),
        ingestion_version=_get("INGESTION_VERSION", "v2"),
        mineru_api_key=_get("MINERU_API_KEY"),
        mineru_base_url=_get("MINERU_BASE_URL"),
        mineru_dump_dir=_get("MINERU_DUMP_DIR"),
    )


def _load_dotenv() -> None:
    """若当前目录存在 .env,加载到 environ(不覆盖已有变量)。"""
    env_file = Path(".env")
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
