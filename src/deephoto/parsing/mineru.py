"""MinerU PDF 异步解析客户端。

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
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import pdf_backend
from .content_list import ContentElement, parse_content_list
from .formats import FormatInfo

logger = logging.getLogger(__name__)

MINERU_DEFAULT_BASE_URL = "https://mineru.net"
# 默认轮询:最多等待约 5 分钟
DEFAULT_POLL_INTERVAL_SECONDS = 3.0
DEFAULT_MAX_WAIT_SECONDS = 300.0
# MinerU 单文件限制:≤200 MB、≤200 页;批量上限 200 个文件。
# 每次只申请一份文件的上传地址(低于单次申请最多 50 个的限制)。
DEFAULT_MAX_PAGES_PER_CHUNK = 200
DEFAULT_MAX_BYTES = 200_000_000
DEFAULT_MAX_CHUNKS = 200
# 结果 ZIP 下载:最多尝试次数与退避基数(秒,指数增长,单次上限 30 秒)
DOWNLOAD_MAX_ATTEMPTS = 4
DOWNLOAD_BACKOFF_SECONDS = 2.0


class MinerUError(RuntimeError):
    """MineU 调用失败或响应格式不正确。"""


@dataclass(frozen=True)
class MinerUResult:
    """按页 markdown(索引即页码-1)。"""
    page_texts: list[str]
    raw: dict[str, Any]
    # 带类型的内容元素(标题/正文/图/表/图表,含原图字节);无 content_list 时为空,入库退回 page_texts
    elements: list[ContentElement] = field(default_factory=list)


class MinerUClient:
    def __init__(self, api_key: str, base_url: str = MINERU_DEFAULT_BASE_URL,
                 poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
                 max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
                 max_pages_per_chunk: int = DEFAULT_MAX_PAGES_PER_CHUNK,
                 max_bytes_per_chunk: int = DEFAULT_MAX_BYTES,
                 opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 dump_dir: str | Path | None = None,
                 on_progress: Callable[[dict], None] | None = None,
                 ocr_disabled: frozenset = frozenset()):
        if not api_key or not api_key.strip():
            raise MinerUError("MineU 需要 API Token")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        if max_pages_per_chunk < 1 or max_bytes_per_chunk < 1:
            raise ValueError("MinerU 分块上限必须大于零")
        self.max_pages_per_chunk = min(max_pages_per_chunk, DEFAULT_MAX_PAGES_PER_CHUNK)
        self.max_bytes_per_chunk = min(max_bytes_per_chunk, DEFAULT_MAX_BYTES)
        self._opener = opener or urlopen
        self._sleep = sleep
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self._on_progress = on_progress
        self.ocr_disabled = ocr_disabled   # 这些格式上传时 is_ocr=False(§3.4a,默认全 True)

    def _emit(self, event: dict) -> None:
        """可选观测回调:只传普通数据(不含签名地址/密钥/原文),回调异常不打断解析。"""
        if self._on_progress is None:
            return
        try:
            self._on_progress(event)
        except Exception:   # noqa: BLE001 - 观测绝不能搞挂业务
            logger.debug("mineru on_progress callback failed", exc_info=True)

    # ---- 对外 ----

    def parse_file(self, data: bytes, filename: str, fmt: FormatInfo) -> MinerUResult:
        """按格式解析文件。PDF 走 parse_pdf(行为逐字节等价);其他格式不拆分。

        非 PDF:Office 无法安全拆分,超限直接报可读错误;页数不由 PDF 引擎推断
        (content_list 的元素 page_idx 兜底,见 _zip_extract),单张图片强制 1 页。
        """
        if fmt.key == "pdf":
            return self.parse_pdf(data, filename)
        if len(data) > self.max_bytes_per_chunk:
            raise MinerUError(
                f"文件超过 MinerU 单文件限制 {self.max_bytes_per_chunk // 1_000_000}MB,"
                "且该格式不支持拆分")
        self._chunk_index = 1
        expected = 1 if fmt.key == "image" else None
        return self._parse_chunk(data, filename, expected_pages=expected, fmt=fmt)

    def parse_pdf(self, pdf_bytes: bytes, filename: str = "document.pdf") -> MinerUResult:
        """按两个单文件限制拆分 PDF,逐块上传,结果按原页序合并。"""
        started = time.monotonic()
        try:
            chunks = pdf_backend.chunk_pdf(
                pdf_bytes, self.max_pages_per_chunk, self.max_bytes_per_chunk,
                max_chunks=DEFAULT_MAX_CHUNKS,
            )
        except pdf_backend.PDFBackendError as exc:
            raise MinerUError(f"PDF 拆分失败: {exc}") from exc
        try:
            # 页数仅供观测:引擎不可用时为 None,绝不让观测影响解析
            pages_total = sum(pdf_backend.page_count(c) for c in chunks)
        except Exception:   # noqa: BLE001
            pages_total = None
        self._emit({"type": "split", "pages": pages_total, "bytes": len(pdf_bytes),
                    "chunks": len(chunks),
                    "duration_ms": int((time.monotonic() - started) * 1000)})
        if len(chunks) == 1:
            self._chunk_index = 1   # 经实例属性传递,保持 _parse_chunk 签名兼容(测试替身)
            return self._parse_chunk(chunks[0], filename)
        # 多块:逐块解析,按页序拼接(页与页的相对顺序保持一致)
        page_texts: list[str] = []
        elements: list[ContentElement] = []
        raws: list[dict[str, Any]] = []
        merge_ms = 0          # 只累计实际合并操作耗时(不含拆分与各块网络往返)
        stem, dot, suffix = filename.rpartition(".")
        for index, chunk in enumerate(chunks):
            chunk_name = f"{stem}_part{index + 1}{dot}{suffix}" if dot else f"{filename}_part{index + 1}"
            self._chunk_index = index + 1
            result = self._parse_chunk(chunk, chunk_name)
            clock = time.monotonic()
            offset = len(page_texts)          # 每块的 page_idx 都从 0 起,合并时按已有页数平移
            elements.extend(replace(el, page_idx=el.page_idx + offset) for el in result.elements)
            page_texts.extend(result.page_texts)
            raws.append(result.raw)
            merge_ms += int((time.monotonic() - clock) * 1000)
        self._emit({"type": "merge_end", "chunks": len(chunks), "pages": len(page_texts),
                    "duration_ms": merge_ms})
        return MinerUResult(page_texts=page_texts, raw={"chunks": raws}, elements=elements)

    # ---- 内部:各步骤 ----

    def _parse_chunk(self, pdf_bytes: bytes, filename: str,
                     expected_pages: int | None | str = "auto",
                     fmt: FormatInfo | None = None) -> MinerUResult:
        # expected_pages="auto":PDF 路径,由本地引擎数页(现状);非 PDF 由调用方显式给(None/1)
        if expected_pages == "auto":
            expected_pages = pdf_backend.page_count(pdf_bytes)
        index = getattr(self, "_chunk_index", 1)
        self._emit({"type": "chunk_start", "index": index, "pages": expected_pages,
                    "bytes": len(pdf_bytes)})
        clock = time.monotonic()
        batch_id, upload_url = self._request_upload_url(filename, fmt)
        self._emit({"type": "request_url_end", "index": index,
                    "duration_ms": int((time.monotonic() - clock) * 1000)})
        clock = time.monotonic()
        self._upload_pdf(upload_url, pdf_bytes)
        self._emit({"type": "upload_end", "index": index, "bytes": len(pdf_bytes),
                    "duration_ms": int((time.monotonic() - clock) * 1000)})
        extract = self._poll_result(batch_id, index)
        clock = time.monotonic()
        zip_bytes = self._download(extract["full_zip_url"])
        self._emit({"type": "download_end", "index": index, "bytes": len(zip_bytes),
                    "duration_ms": int((time.monotonic() - clock) * 1000)})
        self._dump(zip_bytes, filename)
        clock = time.monotonic()
        page_texts, elements = _zip_extract(zip_bytes, expected_pages)
        self._emit({"type": "extract_end", "index": index,
                    "duration_ms": int((time.monotonic() - clock) * 1000),
                    "elements": len(elements),
                    "figures": sum(1 for el in elements if el.image_bytes),
                    "fallback": not any(elements)})
        return MinerUResult(page_texts=page_texts, raw=extract, elements=elements)

    def _dump(self, zip_bytes: bytes, filename: str) -> None:
        """调试:配置 DEEPHOTO_MINERU_DUMP_DIR 后保存云端返回的原始 ZIP,用于核对真实字段格式。"""
        if self.dump_dir is None:
            return
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", filename)
            (self.dump_dir / f"{int(time.time())}_{safe}.zip").write_bytes(zip_bytes)
        except OSError:
            pass   # 调试功能,落盘失败不影响解析

    def _request_upload_url(self, filename: str, fmt: FormatInfo | None = None) -> tuple[str, str]:
        # 参数按格式(§3.4a):HTML 必须 MinerU-HTML;is_ocr 默认 True(样本实测 png/doc/ppt
        # 均正常),born-digital 的 Office 是否需要 OCR 未实测,用 ocr_disabled 按格式关
        model_version = "MinerU-HTML" if (fmt and fmt.key == "html") else "vlm"
        is_ocr = fmt.key not in self.ocr_disabled if fmt else True
        payload = {
            "enable_formula": True,
            "enable_table": True,
            "language": "ch",
            "model_version": model_version,
            "files": [{"name": filename, "is_ocr": is_ocr}],
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

    def _poll_result(self, batch_id: str, index: int = 1) -> dict[str, Any]:
        deadline = time.monotonic() + self.max_wait
        poll_start = time.monotonic()
        attempts = 0
        while True:
            data = self._request_json("GET", f"/api/v4/extract-results/batch/{batch_id}", None)
            attempts += 1
            results = data.get("extract_result")
            first = results[0] if isinstance(results, list) and results else data
            state = str(first.get("state", "")).lower()
            # 云端状态原样记录(诊断用);含义确认只在观测层翻译,这里不做假设
            self._emit({"type": "poll", "index": index, "state": state, "attempts": attempts,
                        "elapsed_ms": int((time.monotonic() - poll_start) * 1000)})
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
        """下载结果 ZIP:失败自动重试并尽量断点续传(Range)。

        大文件(几十 MB)经 CDN 下载时,连接可能中途被断开:http.client 抛
        IncompleteRead(它不是 OSError/URLError,旧实现不会捕获,任务直接失败)。
        此时云端解析早已完成,没必要整份重来,只需重新取回结果:
        - 已收到的字节保留,重试带 Range: bytes=N-;服务器返回 206 则续传,返回 200(忽略 Range)则整份重取;
        - 4xx(如预签名链接过期)不可重试,5xx/网络错误/超时重试,指数退避;
        - 经过重试拼接出的结果先校验 ZIP 完整性,不完整则丢弃重来。"""
        data = b""
        retried = False
        last_error: Exception | None = None
        for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
            if attempt > 1:
                retried = True
                logger.warning("MinerU 结果下载重试 %d/%d(已收到 %d 字节): %s",
                               attempt, DOWNLOAD_MAX_ATTEMPTS, len(data), last_error)
                self._sleep(min(DOWNLOAD_BACKOFF_SECONDS * 2 ** (attempt - 2), 30.0))
            resuming = len(data) > 0
            headers = {"Range": f"bytes={len(data)}-"} if resuming else {}
            status = None
            try:
                response = self._opener(Request(url, method="GET", headers=headers), timeout=120)
                status = getattr(response, "status", None)
                body = response.read()
            except http.client.IncompleteRead as exc:
                # 已读到的部分保留:续传响应(206)接在原数据后,整份响应(200)则替换
                data = data + exc.partial if (resuming and status == 206) else exc.partial
                last_error = exc
                continue
            except HTTPError as exc:
                if exc.code == 416:                      # 续传范围无效:丢弃已收数据整份重取
                    data, last_error = b"", exc
                    continue
                if exc.code < 500:                       # 4xx:链接过期/无权限,重试无意义
                    raise MinerUError(f"MineU 结果下载失败: {exc}") from exc
                last_error = exc
                continue
            except (URLError, OSError, http.client.HTTPException) as exc:   # 含超时、连接重置
                last_error = exc
                continue
            data = data + body if (resuming and status == 206) else body
            if not retried:
                return data
            if _zip_is_complete(data):
                return data
            data, last_error = b"", MinerUError("续传拼接后的 ZIP 不完整")
        raise MinerUError(
            f"MineU 结果下载失败(已重试 {DOWNLOAD_MAX_ATTEMPTS} 次): {last_error}") from last_error

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
        if isinstance(envelope, dict) and (
            envelope.get("success") is False or envelope.get("code", 0) != 0
        ):
            raise MinerUError(f"MineU 错误: {envelope.get('msg') or envelope.get('msgCode')}")
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if data is None:
            raise MinerUError("MineU 响应缺少 data 字段")
        return data


def _zip_is_complete(data: bytes) -> bool:
    """ZIP 中央目录在文件末尾,截断的文件打不开;用来确认重试拼接结果完整。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.testzip() is None
    except zipfile.BadZipFile:
        return False


def _zip_to_page_texts(zip_bytes: bytes, expected_pages: int | None = None) -> list[str]:
    """从结果 ZIP 提取按页文本(兼容入口)。"""
    return _zip_extract(zip_bytes, expected_pages)[0]


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _zip_extract(zip_bytes: bytes, expected_pages: int | None = None) -> tuple[list[str], list[ContentElement]]:
    """从结果 ZIP 提取 (按页文本, 带类型的内容元素)。

    优先 content_list.json(带 page_idx,是真正的按页结构);
    没有时使用带分页符的 full.md;多页结果缺少页码信息时直接报错。
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
        page_texts = _content_list_to_pages(items, expected_pages)
        images = {n: archive.read(n) for n in names
                  if n.lower().endswith(_IMAGE_EXTS) and not n.endswith("/")}
        elements = parse_content_list(items, images)
        # 无 expected_pages(非 PDF)时,页数不能只按"有文字的页"推断:
        # 结尾只有图的页、纯图片输入会被丢掉(build_document 丢弃 page_idx 越界的元素)。
        # 页数取元素 page_idx 与文字页数的大者;PDF 路径由 expected_pages 兜住,不受影响。
        if expected_pages is None and elements:
            inferred = max(el.page_idx for el in elements) + 1
            if inferred > len(page_texts):
                page_texts += [""] * (inferred - len(page_texts))
        return page_texts, elements

    md_name = next((n for n in names if n.endswith("full.md")), None) or \
        next((n for n in names if n.endswith(".md")), None)
    if md_name is None:
        raise MinerUError("MineU 结果 ZIP 中未找到 markdown")
    text = archive.read(md_name).decode("utf-8", errors="replace")
    if "\f" in text:
        pages = [p.strip() for p in text.split("\f")]
        if pages[-1] == "" and (expected_pages is None or len(pages) == expected_pages + 1):
            pages.pop()
        if expected_pages is not None and len(pages) != expected_pages:
            raise MinerUError("MinerU 结果页数与上传的 PDF 不一致")
        return pages, []
    if expected_pages is not None and expected_pages != 1:
        raise MinerUError("MinerU 多页结果缺少按页结构,无法保证原 PDF 页码准确")
    return ([text.strip()] if text.strip() or expected_pages == 1 else []), []


def _content_list_to_pages(items: list[dict], expected_pages: int | None = None) -> list[str]:
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
        try:
            index = int(page_idx)
        except (TypeError, ValueError) as exc:
            raise MinerUError("MinerU 结果中存在无效页码") from exc
        if index < 0:
            raise MinerUError("MinerU 结果中存在负数页码")
        pages.setdefault(index, []).append(text)
    if expected_pages is not None and pages and max(pages) >= expected_pages:
        raise MinerUError("MinerU 结果中的页码超出上传 PDF 范围")
    if not pages and expected_pages is None:
        return []
    count = expected_pages if expected_pages is not None else max(pages) + 1
    return ["\n".join(pages.get(i, [])) for i in range(count)]
