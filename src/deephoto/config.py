"""运行配置:全部来自环境变量,前缀 DEEPHOTO_。

集中在一处读取,业务代码不直接碰 os.environ。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import overload
from urllib.parse import urlparse

_ENV_PREFIX = "DEEPHOTO_"


@overload
def _get(name: str, default: str) -> str: ...
@overload
def _get(name: str, default: None = None) -> str | None: ...
def _get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(_ENV_PREFIX + name, default)


def _get_bool(name: str, default: bool) -> bool:
    """布尔解析:true/false、1/0(兼容 yes/no);不用 bool("false") 这种坑。"""
    raw = _get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "y", "on"}:
        return True
    if value in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(f"环境变量 {_ENV_PREFIX}{name} 必须是 true/false/1/0(可 yes/no),当前值无法解析")


def _get_float(name: str, default: float, *, minimum: float) -> float:
    raw = _get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"环境变量 {_ENV_PREFIX}{name} 必须是数字,当前值无法解析") from None
    if value < minimum:
        raise ValueError(f"环境变量 {_ENV_PREFIX}{name} 必须 >= {minimum}")
    return value


def _get_int(name: str, default: int, *, minimum: int) -> int:
    raw = _get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {_ENV_PREFIX}{name} 必须是整数,当前值无法解析") from None
    if value < minimum:
        raise ValueError(f"环境变量 {_ENV_PREFIX}{name} 必须 >= {minimum}")
    return value


def _get_formats() -> frozenset:
    """DEEPHOTO_ALLOWED_FORMATS:逗号分隔的格式 key 白名单;空/未设置 -> 默认可用集。
    未知 key 报错点名变量(与 DESCRIPTION_* 校验同风格)。"""
    from .parsing.formats import supported_keys
    raw = _get("ALLOWED_FORMATS")
    if raw is None or not raw.strip():
        return frozenset({"pdf", "md", "txt", "docx", "pptx", "doc", "ppt", "xls", "image"})
    keys = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = keys - set(supported_keys())
    if unknown:
        raise ValueError(
            f"环境变量 {_ENV_PREFIX}ALLOWED_FORMATS 含未知格式: {', '.join(sorted(unknown))}"
            f"(支持: {', '.join(supported_keys())})")
    if not keys:
        raise ValueError(f"环境变量 {_ENV_PREFIX}ALLOWED_FORMATS 为空;请至少保留一种格式")
    return frozenset(keys)


def _get_no_ocr_formats() -> frozenset:
    """DEEPHOTO_MINERU_NO_OCR:逗号分隔的格式 key,这些格式走 MinerU 时 is_ocr=False。
    默认空(全部 True,沿用 PDF 现状);born-digital 的 Office 是否受益未实测(§3.4a)。"""
    from .parsing.formats import supported_keys
    raw = _get("MINERU_NO_OCR")
    if raw is None or not raw.strip():
        return frozenset()
    keys = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = keys - set(supported_keys())
    if unknown:
        raise ValueError(
            f"环境变量 {_ENV_PREFIX}MINERU_NO_OCR 含未知格式: {', '.join(sorted(unknown))}"
            f"(支持: {', '.join(supported_keys())})")
    return frozenset(keys)


def _get_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    """枚举配置:值必须在 choices 内,否则报错点名变量(不报值)。"""
    raw = _get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in choices:
        raise ValueError(
            f"环境变量 {_ENV_PREFIX}{name} 必须是 {'/'.join(choices)} 之一,当前值无法识别")
    return value


def _description_url_error(url: str) -> str | None:
    """Base URL 校验;只描述问题,不回显任何值(防泄露)。"""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "必须是合法的 http/https Base URL"
    if parsed.username or parsed.password:
        return "不能包含用户名或密码"
    if parsed.query:
        return "不能包含查询参数"
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        return "请填到 /v1 为止的 Base URL,不要包含 /chat/completions"
    if "{" in url or "}" in url or "YOUR_" in url.upper():
        return "含有未替换的占位符,请按控制台调用示例填写真实地址"
    return None


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

    # 图片描述模型:独立于问答/Embedding/MinerU;默认关闭(显式 DESCRIPTION_ENABLED=true 启用)
    description_enabled: bool = False
    description_api_key: str | None = None
    description_base_url: str | None = None
    description_model: str = "qwen3.8-omni-flash"
    description_reasoning_effort: str | None = "none"   # 空字符串 -> None,不发送该参数
    description_timeout_seconds: float = 90.0
    description_max_retries: int = 1
    description_max_tokens: int = 1024

    # 多格式:上传白名单(formats.FormatInfo.key);默认只开当前有可用引擎的格式
    allowed_formats: frozenset = frozenset(
        {"pdf", "md", "txt", "docx", "pptx", "doc", "ppt", "xls", "image"})
    markdown_data_uri_max_mb: int = 10   # md 内联图(data URI)单张上限
    docx_parser: str = "local"           # local(python-docx)| mineru(云端,耗额度)
    pptx_parser: str = "local"           # local(python-pptx)| mineru(云端,耗额度)
    max_zip_uncompressed_mb: int = 500   # OOXML(zip)解压总量上限(防压缩炸弹)
    mineru_no_ocr_formats: frozenset = frozenset()  # 这些格式走 MinerU 时 is_ocr=False(§3.4a)

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
    settings = Settings(
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
        description_enabled=_get_bool("DESCRIPTION_ENABLED", False),
        description_api_key=_get("DESCRIPTION_API_KEY"),
        description_base_url=_get("DESCRIPTION_BASE_URL"),
        description_model=_get("DESCRIPTION_MODEL", "qwen3.8-omni-flash"),
        description_reasoning_effort=(_get("DESCRIPTION_REASONING_EFFORT", "none") or "").strip() or None,
        description_timeout_seconds=_get_float("DESCRIPTION_TIMEOUT_SECONDS", 90.0, minimum=1.0),
        description_max_retries=_get_int("DESCRIPTION_MAX_RETRIES", 1, minimum=0),
        description_max_tokens=_get_int("DESCRIPTION_MAX_TOKENS", 1024, minimum=1),
        allowed_formats=_get_formats(),
        markdown_data_uri_max_mb=_get_int("MARKDOWN_DATA_URI_MAX_MB", 10, minimum=1),
        docx_parser=_get_choice("DOCX_PARSER", "local", ("local", "mineru")),
        pptx_parser=_get_choice("PPTX_PARSER", "local", ("local", "mineru")),
        mineru_no_ocr_formats=_get_no_ocr_formats(),
        max_zip_uncompressed_mb=_get_int("MAX_ZIP_UNCOMPRESSED_MB", 500, minimum=1),
    )
    validate_description(settings)
    return settings


def validate_description(settings: "Settings") -> None:
    """启用描述时启动校验:报配置变量名,不输出变量值或密钥。关闭时不要求 key/URL。"""
    if not settings.description_enabled:
        return
    problems = []
    if not (settings.description_api_key or "").strip():
        problems.append("DEEPHOTO_DESCRIPTION_API_KEY 为空")
    base_url = (settings.description_base_url or "").strip()
    if not base_url:
        problems.append("DEEPHOTO_DESCRIPTION_BASE_URL 为空")
    elif error := _description_url_error(base_url):
        problems.append(f"DEEPHOTO_DESCRIPTION_BASE_URL {error}")
    if not (settings.description_model or "").strip():
        problems.append("DEEPHOTO_DESCRIPTION_MODEL 为空")
    if problems:
        raise ValueError("图片描述配置不完整: " + "; ".join(problems))


def description_uses_plain_http(settings: "Settings") -> bool:
    """启用且 Base URL 为非本机的 http:// 明文地址(密钥与图片会明文传输)。"""
    if not settings.description_enabled or not settings.description_base_url:
        return False
    parsed = urlparse(settings.description_base_url.strip())
    return parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}


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
