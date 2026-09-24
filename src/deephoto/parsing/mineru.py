"""MineU 整 PDF 异步解析客户端。

流程(对应 https://mineru.net/apiManage/docs 的 v4 接口):
  1) POST /file-urls/batch  申请上传地址,得到 batch_id 与预签名 URL;
  2) PUT  把 PDF 字节上传到该 URL;
  3) GET  /extract-results/batch/{batch_id} 轮询,直到 done/failed;
  4) 成功后下载 full_zip_url,解出按页 markdown。

仅依赖标准库,便于在无 SDK 的环境运行;所有网络调用可注入 opener 以便测试。
"""

from __future__ import annotations

import http.client
import io
import json
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import pdf_backend

MINERU_DEFAULT_BASE_URL = "https://mineru.net"
# 默认轮询:最多等待约 5 分钟
DEFAULT_POLL_INTERVAL_SECONDS = 3.0
DEFAULT_MAX_WAIT_SECONDS = 300.0
# MineU 单文件限制:≤200MB、≤200 页(见 mineru.net/apiManage/docs)。
# 超过 200 页的 PDF 先拆成每块至多该页数再逐块解析。
DEFAULT_MAX_PAGES_PER_CHUNK = 200
DEFAULT_MAX_BYTES = 200 * 1024 * 1024


class MinerUError(RuntimeError):
    """MineU 调用失败或响应格式不正确。"""


@dataclass(frozen=True)
class MinerUResult:
    """按页 markdown(索引即页码-1)。"""
    page_texts: list[str]
    raw: dict[str, Any]


class MinerUClient:
    def __init__(self, api_key: str, base_url: str = MINERU_DEFAULT_BASE_URL,
                 poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
                 max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
                 max_pages_per_chunk: int = DEFAULT_MAX_PAGES_PER_CHUNK,
                 opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key or not api_key.strip():
            raise MinerUError("MineU 需要 API Token")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.max_pages_per_chunk = max(1, max_pages_per_chunk)
        self._opener = opener or urlopen
        self._sleep = sleep

    # ---- 对外 ----

    def parse_pdf(self, pdf_bytes: bytes, filename: str = "document.pdf") -> MinerUResult:
        """解析整份 PDF;超过单文件页数上限时自动分块并按页序合并结果。"""
        if len(pdf_bytes) > DEFAULT_MAX_BYTES:
            raise MinerUError(
                f"PDF 超过 MineU 单文件大小限制({DEFAULT_MAX_BYTES // (1024 * 1024)}MB)")
        chunks = pdf_backend.chunk_pdf(pdf_bytes, self.max_pages_per_chunk)
        if len(chunks) == 1:
            return self._parse_chunk(chunks[0], filename)
        # 多块:逐块解析,按页序拼接(页与页的相对顺序保持一致)
        page_texts: list[str] = []
        raws: list[dict[str, Any]] = []
        stem, dot, suffix = filename.rpartition(".")
        for index, chunk in enumerate(chunks):
            chunk_name = f"{stem}_part{index + 1}{dot}{suffix}" if dot else f"{filename}_part{index + 1}"
            result = self._parse_chunk(chunk, chunk_name)
            page_texts.extend(result.page_texts)
            raws.append(result.raw)
        return MinerUResult(page_texts=page_texts, raw={"chunks": raws})

    # ---- 内部:各步骤 ----

    def _parse_chunk(self, pdf_bytes: bytes, filename: str) -> MinerUResult:
        batch_id, upload_url = self._request_upload_url(filename)
        self._upload_pdf(upload_url, pdf_bytes)
        extract = self._poll_result(batch_id)
        zip_bytes = self._download(extract["full_zip_url"])
        return MinerUResult(page_texts=_zip_to_page_texts(zip_bytes), raw=extract)

    def _request_upload_url(self, filename: str) -> tuple[str, str]:
        payload = {
            "enable_formula": True,
            "enable_table": True,
            "language": "ch",
            "model_version": "vlm",
            "files": [{"name": filename, "is_ocr": True}],
        }
        data = self._request_json("POST", "/api/v4/file-urls/batch", payload)
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls")
        if not batch_id or not isinstance(file_urls, list) or not file_urls:
            raise MinerUError("MineU 未返回有效的上传地址")
        return str(batch_id), str(file_urls[0])

    def _upload_pdf(self, upload_url: str, pdf_bytes: bytes) -> None:
        # OSS 预签名 URL 对头部敏感:urllib 会自动塞 Content-Type 导致 403。
        # 用 http.client 裸 PUT,只带 Host / Content-Length。
        parsed = urlparse(upload_url)
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=120)
        try:
            path = parsed.path + (("?" + parsed.query) if parsed.query else "")
            conn.putrequest("PUT", path, skip_accept_encoding=True)
            conn.putheader("Content-Length", str(len(pdf_bytes)))
            conn.endheaders(pdf_bytes)
            response = conn.getresponse()
            response.read()
            if response.status >= 400:
                raise MinerUError(f"MineU 文件上传失败: HTTP {response.status}")
        except (OSError, http.client.HTTPException) as exc:
            raise MinerUError(f"MineU 文件上传失败: {exc}") from exc
        finally:
            conn.close()

    def _poll_result(self, batch_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.max_wait
        while True:
            data = self._request_json("GET", f"/api/v4/extract-results/batch/{batch_id}", None)
            results = data.get("extract_result")
            first = results[0] if isinstance(results, list) and results else data
            state = str(first.get("state", "")).lower()
            if state == "done":
                if not first.get("full_zip_url"):
                    raise MinerUError("MineU 完成但缺少结果下载地址")
                return first
            if state in {"failed", "error"}:
                raise MinerUError(f"MineU 解析失败: {first.get('err_msg') or state}")
            if time.monotonic() >= deadline:
                raise MinerUError("MineU 解析超时")
            self._sleep(self.poll_interval)

    def _download(self, url: str) -> bytes:
        request = Request(url, method="GET")
        try:
            response = self._opener(request, timeout=120)
            return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            raise MinerUError(f"MineU 结果下载失败: {exc}") from exc

    def _request_json(self, method: str, path: str, payload: dict | None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(self.base_url + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })
        try:
            response = self._opener(request, timeout=60)
            raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise MinerUError(f"MineU 返回 HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise MinerUError(f"无法连接 MineU: {exc}") from exc
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MinerUError("MineU 返回的不是有效 JSON") from exc
        if isinstance(envelope, dict) and envelope.get("success") is False:
            raise MinerUError(f"MineU 错误: {envelope.get('msg') or envelope.get('msgCode')}")
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if data is None:
            raise MinerUError("MineU 响应缺少 data 字段")
        return data


def _zip_to_page_texts(zip_bytes: bytes) -> list[str]:
    """从结果 ZIP 提取按页文本。

    优先 content_list.json(带 page_idx,是真正的按页结构);
    没有时退化用 full.md(MineU 不按 \f 分页时只能整篇一页)。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise MinerUError("MineU 结果 ZIP 无法解压") from exc
    names = archive.namelist()

    content_name = next((n for n in names if n.endswith("content_list.json")), None)
    if content_name is not None:
        try:
            items = json.loads(archive.read(content_name).decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise MinerUError("MineU content_list.json 不是有效 JSON") from exc
        return _content_list_to_pages(items)

    md_name = next((n for n in names if n.endswith("full.md")), None) or \
        next((n for n in names if n.endswith(".md")), None)
    if md_name is None:
        raise MinerUError("MineU 结果 ZIP 中未找到 markdown")
    text = archive.read(md_name).decode("utf-8", errors="replace")
    if "\f" in text:
        return [p.strip() for p in text.split("\f")]
    return [text.strip()] if text.strip() else []


def _content_list_to_pages(items: list[dict]) -> list[str]:
    """把 content_list 的元素按 page_idx 聚成逐页文本。"""
    if not isinstance(items, list):
        return []
    pages: dict[int, list[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        page_idx = item.get("page_idx")
        text = str(item.get("text") or "").strip()
        if page_idx is None or not text:
            continue
        pages.setdefault(int(page_idx), []).append(text)
    if not pages:
        return []
    count = max(pages) + 1
    return ["\n".join(pages.get(i, [])) for i in range(count)]
