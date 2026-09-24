"""PDF 读取/渲染后端:自动选择可用引擎。

引擎优先级:
  1) poppler(pdftotext/pdftoppm/pdfinfo)—— Linux/WSL 常用,无需 Python 依赖;
  2) PyMuPDF(pymupdf)—— Windows 上 poppler 难装时的回退。

对上层(解析器/入库)只暴露统一函数:page_count / page_texts /
render_page / render_region,签名与坐标约定不变(bbox 为 PDF 点坐标)。
运行环境缺哪个引擎都不影响另一个被选用;两者都缺时才抛 PDFBackendError。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# 渲染分辨率:150 DPI 足够页预览与喂给 OCR
_RENDER_DPI = 150

_pdftotext = shutil.which("pdftotext")
_pdftoppm = shutil.which("pdftoppm")
_pdfinfo = shutil.which("pdfinfo")

try:  # PyMuPDF 作为回退引擎;Windows 上 poppler 通常不可用
    import pymupdf as _fitz
    _HAS_PYMUPDF = True
except Exception:  # pragma: no cover - 取决于部署环境
    _fitz = None
    _HAS_PYMUPDF = False


class PDFBackendError(RuntimeError):
    """没有可用的 PDF 引擎,或引擎调用失败。"""


def poppler_available() -> bool:
    return _pdftotext is not None and _pdftoppm is not None


def pymupdf_available() -> bool:
    return _HAS_PYMUPDF


def backend_name() -> str:
    if poppler_available():
        return "poppler"
    if pymupdf_available():
        return "pymupdf"
    return "none"


def _require() -> None:
    if not poppler_available() and not pymupdf_available():
        raise PDFBackendError(
            "缺少 PDF 引擎:请安装 poppler(pdftotext/pdftoppm)或 pymupdf")


def _with_pdf(pdf_bytes: bytes, fn):
    """把字节写入临时 .pdf,调用 fn(path) 后清理(poppler 需要文件路径)。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "input.pdf"
        path.write_bytes(pdf_bytes)
        return fn(path)


# ---- 统一接口:优先 poppler,回退 PyMuPDF ----

def page_count(pdf_bytes: bytes) -> int:
    _require()
    if poppler_available():
        return _poppler_page_count(pdf_bytes)
    return _pymupdf_page_count(pdf_bytes)


def page_texts(pdf_bytes: bytes) -> list[str]:
    """逐页返回文本(索引即页码-1)。"""
    _require()
    if poppler_available():
        return _poppler_page_texts(pdf_bytes)
    return _pymupdf_page_texts(pdf_bytes)


def render_page(pdf_bytes: bytes, page_number: int, dpi: int = _RENDER_DPI) -> bytes:
    """渲染整页为 PNG 字节(1 起页码)。"""
    _require()
    if poppler_available():
        return _poppler_render(pdf_bytes, page_number, None, dpi)
    return _pymupdf_render(pdf_bytes, page_number, None, dpi)


def render_region(pdf_bytes: bytes, page_number: int, bbox, dpi: int = _RENDER_DPI) -> bytes:
    """渲染某页指定区域(bbox 为 PDF 点坐标 x0,y0,x1,y1)为 PNG。"""
    _require()
    if poppler_available():
        return _poppler_render(pdf_bytes, page_number, bbox, dpi)
    return _pymupdf_render(pdf_bytes, page_number, bbox, dpi)


def chunk_pdf(pdf_bytes: bytes, max_pages: int) -> list[bytes]:
    """把 PDF 按 max_pages 拆成若干子 PDF(供 MineU ≤200 页限制的分块上传)。

    拆分需要 PyMuPDF(poppler 无写 PDF 能力);页数不超过 max_pages 时直接
    返回原字节单元素列表,不产生额外拷贝。块内页序与原 PDF 一致。
    """
    if not pymupdf_available():
        raise PDFBackendError("拆分 PDF 需要 PyMuPDF(pypdf/pymupdf)")
    src = _fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = src.page_count
        if total <= max_pages:
            return [pdf_bytes]
        chunks: list[bytes] = []
        for start in range(0, total, max_pages):
            end = min(start + max_pages - 1, total - 1)
            out = _fitz.open()
            try:
                out.insert_pdf(src, from_page=start, to_page=end)
                chunks.append(out.tobytes())
            finally:
                out.close()
        return chunks
    finally:
        src.close()


# ---- poppler 实现 ----

def _poppler_page_count(pdf_bytes: bytes) -> int:
    def run(path: Path) -> int:
        if _pdfinfo:
            out = subprocess.run([_pdfinfo, str(path)], capture_output=True, timeout=60)
            if out.returncode == 0:
                for line in out.stdout.decode("utf-8", errors="replace").splitlines():
                    if line.lower().startswith("pages:"):
                        return int(line.split(":", 1)[1].strip())
        out = subprocess.run([_pdftotext, str(path), "-"], capture_output=True, timeout=120)
        if out.returncode != 0:
            raise PDFBackendError(f"pdftotext 失败: {out.stderr.decode(errors='replace')[:200]}")
        text = out.stdout.decode("utf-8", errors="replace")
        return max(1, text.count("\f")) if text else 0

    return _with_pdf(pdf_bytes, run)


def _poppler_page_texts(pdf_bytes: bytes) -> list[str]:
    def run(path: Path) -> list[str]:
        out = subprocess.run(
            [_pdftotext, "-layout", str(path), "-"],
            capture_output=True, timeout=180,
        )
        if out.returncode != 0:
            raise PDFBackendError(f"pdftotext 失败: {out.stderr.decode(errors='replace')[:200]}")
        text = out.stdout.decode("utf-8", errors="replace")
        pages = text.split("\f")
        return pages[:-1] if pages and pages[-1] == "" else pages

    return _with_pdf(pdf_bytes, run)


def _poppler_render(pdf_bytes: bytes, page_number: int, bbox, dpi: int) -> bytes:
    def run(path: Path) -> bytes:
        out_prefix = Path(path.parent) / "out"
        cmd = [_pdftoppm, "-png", "-r", str(dpi),
               "-f", str(page_number), "-l", str(page_number)]
        if bbox is not None:
            scale = dpi / 72.0
            cmd += ["-x", str(int(bbox[0] * scale)),
                    "-y", str(int(bbox[1] * scale)),
                    "-W", str(max(1, int((bbox[2] - bbox[0]) * scale))),
                    "-H", str(max(1, int((bbox[3] - bbox[1]) * scale)))]
        cmd += ["-singlefile", str(path), str(out_prefix)]
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
        if proc.returncode != 0:
            raise PDFBackendError(f"pdftoppm 失败: {proc.stderr.decode(errors='replace')[:200]}")
        png = out_prefix.with_suffix(".png")
        if not png.is_file():
            raise PDFBackendError(f"pdftoppm 未生成第 {page_number} 页图像")
        return png.read_bytes()

    return _with_pdf(pdf_bytes, run)


# ---- PyMuPDF 实现 ----

def _pymupdf_page_count(pdf_bytes: bytes) -> int:
    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


def _pymupdf_page_texts(pdf_bytes: bytes) -> list[str]:
    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [doc.load_page(i).get_text("text") for i in range(doc.page_count)]
    finally:
        doc.close()


def _pymupdf_render(pdf_bytes: bytes, page_number: int, bbox, dpi: int) -> bytes:
    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc.load_page(page_number - 1)
        zoom = dpi / 72.0
        clip = _fitz.Rect(*bbox) if bbox is not None else None
        pix = page.get_pixmap(matrix=_fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()
