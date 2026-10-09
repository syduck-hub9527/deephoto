"""知识库虚拟文件系统(开发文档 02-backends-and-knowledge-vfs)。

把"当前租户可检索的 ready 文档"投影成一个**只读**的虚拟目录树,挂在 CompositeBackend 的 /kb/ 下,
让智能体可以用 deepagents 内置的 ls / read_file / glob / grep 做"精确字面检索"与"顺序通读":

    /kb/index.md                  文档清单(id、文件名、格式、位置语义)
    /kb/<document_id>/content.md  全文:按阅读顺序排列的块,每块以 "### [chunk:ID] 位置" 开头
    /kb/<document_id>/figures.md  图片清单:每张图以 "### [image:ID] 图号 · 位置" 开头(只有文字描述,不含像素)

设计要点:
- 只读:write/edit/delete/upload 一律返回错误;profile 另外把写类工具对模型隐藏(双保险)。
- 租户隔离:实例按请求构造并绑定 AuthContext,可见文档只来自 KnowledgeService.allowed_document_ids。
- 引用校验不变:read/grep 实际返回给模型的块/图 ID 会写入 tracker,_assemble 的校验逻辑无需改动。
- read 的窗口起点会回退到所在块的块头,保证模型读到正文时一定同时看到可引用的 ID。
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from typing import Callable
from sqlite3 import Connection

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    InvalidGlobPatternError,
    _glob_search_files,
    create_file_data,
    grep_matches_from_files,
    normalize_read_bounds,
)

from .. import repo
from ..security import AuthContext
from .locator import locator_label

logger = logging.getLogger(__name__)

KB_ROUTE = "/kb/"          # CompositeBackend 路由前缀
_READ_ONLY = "知识库是只读的,不能写入、编辑或删除文件。"


@dataclass
class _Span:
    start: int                      # 0 起的行下标(含)
    end: int                        # 不含
    chunk_id: str | None = None
    image_ids: tuple[str, ...] = ()


@dataclass
class _Rendered:
    lines: list[str]
    spans: list[_Span] = field(default_factory=list)

    @property
    def content(self) -> str:
        return "\n".join(self.lines)

    def span_at(self, idx: int) -> _Span | None:
        for s in self.spans:
            if s.start <= idx < s.end:
                return s
        return None


class KnowledgeVFS(BackendProtocol):
    """租户级只读知识库文件系统。路径均相对挂载点(Composite 剥掉 /kb/ 前缀后传入)。"""

    def __init__(self, knowledge, ctx: AuthContext, connect_fn: Callable[[], Connection],
                 tracker: dict | None = None, *, grep_max_docs: int = 300):
        self._grep_max_docs = grep_max_docs
        self._knowledge = knowledge
        self._ctx = ctx
        self._connect = connect_fn
        self._tracker = tracker if tracker is not None else {"chunks": set(), "images": set()}
        self._docs: dict[str, dict] | None = None
        self._rendered: dict[str, _Rendered] = {}

    # ---- 文档集合与渲染 ----

    def _documents(self) -> dict[str, dict]:
        if self._docs is None:
            conn = self._connect()
            allowed = set(self._knowledge.allowed_document_ids(conn, self._ctx))
            rows = [d for d in repo.list_documents(conn, self._ctx.tenant_id) if d["id"] in allowed]
            self._docs = {d["id"]: d for d in sorted(rows, key=lambda d: (d["filename"], d["id"]))}
        return self._docs

    def _render_index(self) -> _Rendered:
        lines = ["# 知识库文档清单", "",
                 "每个文档目录下有 content.md(全文)和 figures.md(图片清单)。引用证据时使用块头/图头里的 ID。", ""]
        for d in self._documents().values():
            lines.append(f"- {d['id']} · {d['filename']} · 格式 {d['source_format']} · "
                         f"{_size_label(d)} · /kb/{d['id']}/content.md")
        if not self._documents():
            lines.append("(当前没有可检索的文档)")
        return _Rendered(lines)

    def _render_content(self, doc: dict) -> _Rendered:
        conn = self._connect()
        kind = doc["locator_kind"]
        occs = {o["id"]: o for o in repo.occurrences_for_document(conn, doc["id"])}
        lines = [f"# {doc['filename']}", f"文档 ID:{doc['id']} · 格式 {doc['source_format']} · {_size_label(doc)}", ""]
        spans: list[_Span] = []
        for c in repo.chunks_in_order(conn, doc["id"]):
            start = len(lines)
            label = locator_label(kind, c["page_start"], c["page_end"], c["section"])
            lines.append(f"### [chunk:{c['id']}] {label}")
            lines.extend(c["text"].split("\n"))
            image_ids = tuple(i for i in c["referenced_image_ids"] if i in occs)
            if image_ids:
                lines.append("关联图片:" + "、".join(
                    f"[image:{i}]" + (f"(图 {occs[i]['figure_number']})" if occs[i]["figure_number"] else "")
                    for i in image_ids))
            lines.append("")
            spans.append(_Span(start, len(lines), chunk_id=c["id"], image_ids=image_ids))
        return _Rendered(lines, spans)

    def _render_figures(self, doc: dict) -> _Rendered:
        conn = self._connect()
        kind = doc["locator_kind"]
        lines = [f"# {doc['filename']} · 图片清单", "以下只是文字描述;核对图内细节必须用 inspect_image 看原图。", ""]
        spans: list[_Span] = []
        for o in repo.occurrences_for_document(conn, doc["id"]):
            start = len(lines)
            fig = f"图 {o['figure_number']}" if o["figure_number"] else "无图号"
            lines.append(f"### [image:{o['id']}] {fig} · {locator_label(kind, o['page_number'])}")
            lines.append(f"图注:{o['caption'] or '无'}")
            lines.append(f"描述:{o['description'] or '无'}")
            if o["visible_labels"]:
                lines.append("图中可见文字:" + "、".join(map(str, o["visible_labels"])))
            if o["needs_review"]:
                lines.append("注意:该图的描述待人工复核。")
            lines.append("")
            spans.append(_Span(start, len(lines), image_ids=(o["id"],)))
        if not spans:
            lines.append("(该文档没有图片)")
        return _Rendered(lines, spans)

    def _resolve(self, path: str) -> tuple[str, _Rendered] | None:
        """把虚拟路径映射到渲染结果;不存在/越权返回 None。"""
        path = _norm(path)
        if path == "/index.md":
            return path, self._rendered.setdefault("index", self._render_index())
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[1] not in ("content.md", "figures.md"):
            return None
        doc = self._documents().get(parts[0])
        if doc is None:
            return None
        key = f"{parts[0]}/{parts[1]}"
        if key not in self._rendered:
            self._rendered[key] = (self._render_content(doc) if parts[1] == "content.md"
                                   else self._render_figures(doc))
        return path, self._rendered[key]

    def _all_paths(self) -> list[str]:
        paths = ["/index.md"]
        for doc_id in self._documents():
            paths += [f"/{doc_id}/content.md", f"/{doc_id}/figures.md"]
        return paths

    # ---- 证据登记 ----

    def _register(self, rendered: _Rendered, start: int, end: int) -> None:
        """登记 [start, end) 行范围内实际交给模型的块/图 ID(只增不减,_assemble 据此校验引用)。"""
        for s in rendered.spans:
            if s.end <= start or s.start >= end:
                continue
            if s.chunk_id:
                self._tracker["chunks"].add(s.chunk_id)
            for i in s.image_ids:
                self._tracker["images"].add(i)

    # ---- BackendProtocol:读 ----

    def ls(self, path: str) -> LsResult:
        path = _norm(path)
        docs = self._documents()
        if path == "/":
            entries: list[FileInfo] = [{"path": "/index.md", "is_dir": False, "size": 0, "modified_at": ""}]
            entries += [{"path": f"/{d}/", "is_dir": True, "size": 0, "modified_at": ""} for d in docs]
            return LsResult(entries=entries)
        doc_id = path.strip("/")
        if "/" in doc_id or doc_id not in docs:
            return LsResult(error=f"Directory '{path}' not found")
        entries = []
        for name in ("content.md", "figures.md"):
            _, r = self._resolve(f"/{doc_id}/{name}")  # type: ignore[misc]
            entries.append({"path": f"/{doc_id}/{name}", "is_dir": False,
                            "size": len(r.content.encode("utf-8")), "modified_at": ""})
        return LsResult(entries=entries)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        hit = self._resolve(file_path)
        if hit is None:
            return ReadResult(error=f"File '{file_path}' not found")
        _, r = hit
        offset, limit = normalize_read_bounds(offset, limit)
        if limit == 0:
            return ReadResult(file_data=create_file_data(""), no_lines_requested=True)
        total = len(r.lines)
        if offset >= total:
            return ReadResult(error=f"Line offset {offset} exceeds file length ({total} lines)")
        # 窗口起点回退到所在块的块头:读到正文就一定带着可引用的 ID
        span = r.span_at(offset)
        start = span.start if span else offset
        end = min(max(offset + limit, start + 1), total)
        self._register(r, start, end)
        return ReadResult(
            file_data=create_file_data("\n".join(r.lines[start:end])),
            total_lines=total, start_line=start + 1, end_line=end,
            next_offset=end if end < total else None,
        )

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None,
             *, max_count: int | None = None) -> GrepResult:
        norm = _norm(path)
        # 只渲染 path 范围内的文件,避免为一次 grep 加载整个租户的全部文档
        scope = [p for p in self._all_paths()
                 if norm == "/" or p == norm or p.startswith(norm.rstrip("/") + "/")]
        n_docs = len({p.split("/")[1] for p in scope if p != "/index.md"})
        if n_docs > self._grep_max_docs:
            # 每次 grep 都要把范围内的文档整篇渲染进内存;超限时让模型先收窄,而不是悄悄变慢
            return GrepResult(error=f"范围内有 {n_docs} 个文档,超过单次 grep 上限 {self._grep_max_docs};"
                                    "请把 path 限定到某个文档(/kb/<document_id>/content.md)。")
        files: dict[str, dict] = {}
        for p in scope:
            hit = self._resolve(p)
            if hit:
                files[p] = create_file_data(hit[1].content)
        result = grep_matches_from_files(files, pattern, norm, glob, max_count=max_count)
        for m in result.matches or []:
            hit = self._resolve(m["path"])
            if hit:
                self._register(hit[1], m["line"] - 1, m["line"])
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        stubs = {p: create_file_data("") for p in self._all_paths()}
        try:
            found = _glob_search_files(stubs, pattern, path)
        except InvalidGlobPatternError as exc:
            return GlobResult(error=str(exc))
        if found == "No files found":
            return GlobResult(matches=[])
        return GlobResult(matches=[{"path": p, "is_dir": False, "size": 0, "modified_at": ""}
                                   for p in found.split("\n")])

    # ---- BackendProtocol:写(全部拒绝)----

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READ_ONLY)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:  # noqa: FBT001, FBT002
        return EditResult(error=_READ_ONLY)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=_READ_ONLY)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=p, error="permission_denied") for p, _ in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        out = []
        for p in paths:
            hit = self._resolve(p)
            out.append(FileDownloadResponse(path=p, error="file_not_found") if hit is None
                       else FileDownloadResponse(path=p, content=hit[1].content.encode("utf-8")))
        return out


def _norm(path: str | None) -> str:
    """规整为以 / 开头的绝对路径;含 .. 的路径按 posix 规则折叠且不能逃出根。"""
    p = posixpath.normpath("/" + (path or "/").lstrip("/"))
    return "/" if p in ("", ".", "//") else p


def _size_label(doc: dict) -> str:
    n = doc.get("page_count") or 0
    unit = {"slide": "张幻灯片", "sheet": "个工作表", "section": "段"}.get(doc.get("locator_kind"), "页")
    return f"共 {n} {unit}" if n else "页数未知"
