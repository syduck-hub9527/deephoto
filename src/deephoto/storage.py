"""本地文件系统对象存储:内容寻址(sha256),同内容只存一份。

对象键规则:{前缀}/{sha256 前两位}/{完整 sha256}.{扩展名}
对应开发文档§2"对象存储"部件;换 OSS 时只需替换本模块。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_EXT_BY_MIME = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


class ObjectStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sha256_of(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def put(self, data: bytes, prefix: str, mime_type: str) -> tuple[str, str]:
        """写入并返回 (object_key, sha256)。已存在同内容对象时直接复用。"""
        digest = self.sha256_of(data)
        ext = _EXT_BY_MIME.get(mime_type, "bin")
        key = f"{prefix}/{digest[:2]}/{digest}.{ext}"
        path = self.root / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return key, digest

    def get(self, object_key: str) -> bytes:
        return (self.root / object_key).read_bytes()

    def path_of(self, object_key: str) -> Path:
        return self.root / object_key

    def exists(self, object_key: str) -> bool:
        return (self.root / object_key).exists()

    def delete_if_unreferenced(self, object_key: str) -> None:
        path = self.root / object_key
        if path.exists():
            path.unlink()
