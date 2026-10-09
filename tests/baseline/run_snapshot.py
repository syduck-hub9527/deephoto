"""B4 对照基线快照(md文档 00 §4.1)。

用固定问题集(questions.json)跑当前 QAService.answer(非流式),保存回答文本、
[chunk:ID] 引用、图片引用与工具调用序列,作为后续六个方向改造的对照基线。

在哪个环境跑,记录的就是哪个框架版本的行为:
  venv/Scripts/python.exe tests/baseline/run_snapshot.py   # Windows 环境(升级前)
  .venv/bin/python tests/baseline/run_snapshot.py          # Linux 环境(升级后)

输出写入 tests/baseline/snapshots/(gitignored),文件名带时间戳与 deepagents 版本。
模型温度为 1,输出有随机性:快照用于人工比对回答质量与引用格式,不做字节级 diff。
questions.json 里 q6 的 document_id 绑定当前 data/deephoto.db 中的文档,换库需更新。
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from deephoto.agent.knowledge import KnowledgeService  # noqa: E402
from deephoto.agent.qa import QAService  # noqa: E402
from deephoto.config import load_settings  # noqa: E402
from deephoto.db import connect  # noqa: E402
from deephoto.indexing.service import IndexService  # noqa: E402
from deephoto.llm import build_embeddings  # noqa: E402
from deephoto.security import LOCAL_CTX  # noqa: E402
from deephoto.storage import ObjectStore  # noqa: E402

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def _pkg_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _build_qa(settings) -> tuple[QAService, KnowledgeService]:
    """与 api/app.py 同一套接线(只取问答所需部分,不起入库 worker)。"""
    store = ObjectStore(settings.object_dir)
    embeddings = build_embeddings(settings)
    index_service = IndexService(
        embeddings=embeddings,
        embedding_version=settings.embedding_model if embeddings else None,
    )
    knowledge = KnowledgeService(store, index_service)
    return QAService(settings, knowledge), knowledge


def _spy_on_tools(knowledge: KnowledgeService, box: dict) -> None:
    """包埋三个工具背后的服务方法,按调用顺序记录参数与返回的证据 ID。

    box["log"] 指向当前题目的记录列表,每题开始前由调用方重置。
    """
    orig_search = knowledge.search
    orig_read = knowledge.read_chunk
    orig_blocks = knowledge.image_content_blocks

    def search(conn, ctx, query, document_id=None):
        result = orig_search(conn, ctx, query, document_id)
        box["log"].append({
            "tool": "search_knowledge", "query": query, "document_id": document_id or None,
            "chunk_ids": [c["chunk_id"] for c in result.get("chunks", [])],
            "image_ids": [i["image_occurrence_id"] for i in result.get("images", [])],
        })
        return result

    def read_chunk(conn, ctx, chunk_id, neighbors=0):
        result = orig_read(conn, ctx, chunk_id, neighbors)
        box["log"].append({
            "tool": "read_chunk", "chunk_id": chunk_id, "neighbors": neighbors,
            "chunk_ids": [c["chunk_id"] for c in result.get("chunks", [])],
        })
        return result

    def image_blocks(conn, ctx, occ_id):
        blocks = orig_blocks(conn, ctx, occ_id)
        box["log"].append({
            "tool": "inspect_image", "image_occurrence_id": occ_id,
            "has_image": any(b.get("type") == "image" for b in blocks),
        })
        return blocks

    knowledge.search = search
    knowledge.read_chunk = read_chunk
    knowledge.image_content_blocks = image_blocks


def _spy_on_vfs(box: dict) -> None:
    """02 知识库文件系统的调用记录:与知识库工具 spy 共用同一份顺序日志。

    记录的是后端实际收到的 read/grep(ls/glob 省略,价值低);grep 的 output_mode
    由中间件处理、后端不可见,但 grep 命中行号与后续 read 的 offset 是否对齐
    (offset=命中行号-1)可以从日志里验证。
    """
    from deephoto.agent.kb_vfs import KnowledgeVFS

    orig_read = KnowledgeVFS.read
    orig_grep = KnowledgeVFS.grep

    def read(self, file_path, offset=0, limit=2000):
        result = orig_read(self, file_path, offset, limit)
        box["log"].append({
            "tool": "read_file", "path": file_path, "offset": offset, "limit": limit,
            "start_line": result.start_line, "end_line": result.end_line,
            "error": result.error,
        })
        return result

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        result = orig_grep(self, pattern, path, glob, max_count=max_count)
        matches = result.matches or []
        box["log"].append({
            "tool": "grep", "pattern": pattern, "path": path, "glob": glob,
            "matches": len(matches),
            "hit_lines": [{"path": m["path"], "line": m["line"]} for m in matches[:20]],
            "error": result.error,
        })
        return result

    KnowledgeVFS.read = read
    KnowledgeVFS.grep = grep


def main() -> None:
    # 可选参数:问题集文件名(默认 questions.json);02 评估用 questions-kb.json 等
    questions_file = sys.argv[1] if len(sys.argv) > 1 else "questions.json"
    questions = json.loads(
        (Path(__file__).parent / questions_file).read_text(encoding="utf-8"))
    settings = load_settings()
    qa, knowledge = _build_qa(settings)
    conn = connect(settings.db_path)
    box: dict = {"log": []}
    _spy_on_tools(knowledge, box)
    if getattr(settings, "qa_kb_vfs_enabled", False):
        _spy_on_vfs(box)

    records = []
    for i, item in enumerate(questions, 1):
        box["log"] = []
        started = time.monotonic()
        print(f"[{i}/{len(questions)}] {item['id']}: {item['question'][:30]}...", flush=True)
        try:
            result = qa.answer(conn, LOCAL_CTX, item["question"],
                               document_id=item.get("document_id"))
        except Exception as exc:  # 快照要记录失败而不是中断整批
            result = {"error": f"{type(exc).__name__}: {exc}", "answer": None,
                      "citations": [], "images": []}
        elapsed = round(time.monotonic() - started, 1)
        records.append({
            "id": item["id"], "question": item["question"],
            "document_id": item.get("document_id"), "expect": item.get("expect"),
            "elapsed_s": elapsed,
            "tool_calls": box["log"],
            "answer": result.get("answer"),
            "error": result.get("error"),
            "tool_trace": result.get("tool_trace"),
            "citations": [
                {"chunk_id": c["chunk_id"], "page": c["page"], "page_end": c["page_end"],
                 "label": c["label"]}
                for c in result.get("citations", [])
            ],
            "images": [
                {"image_occurrence_id": im["image_occurrence_id"],
                 "figure_number": im["figure_number"], "page": im["page"]}
                for im in result.get("images", [])
            ],
        })
        print(f"    {elapsed}s, {len(box['log'])} 次工具调用, "
              f"{len(records[-1]['citations'])} 条引用, {len(records[-1]['images'])} 张图"
              + (f", 错误: {records[-1]['error']}" if records[-1]["error"] else ""), flush=True)

    versions = {name: _pkg_version(name) for name in
                ("deepagents", "langchain", "langchain-core", "langchain-openai", "langgraph")}
    flags = {"qa_subagents_enabled": getattr(settings, "qa_subagents_enabled", False),
             "qa_kb_vfs_enabled": getattr(settings, "qa_kb_vfs_enabled", False),
             "qa_middleware_enabled": getattr(settings, "qa_middleware_enabled", False),
             "qa_main_max_model_calls": getattr(settings, "qa_main_max_model_calls", 12)}
    snapshot = {
        "meta": {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "python": platform.python_version(),
            "platform": platform.system(),
            "packages": versions,
            "chat_model": settings.chat_model,
            # 01/02 的开关是行为变量,必须随快照记录,否则 A/B 对比会混淆
            **flags,
        },
        "questions": records,
    }
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    suffix = ("-subagents" if flags["qa_subagents_enabled"] else "") + \
             ("-kbvfs" if flags["qa_kb_vfs_enabled"] else "") + \
             ("-middleware" if flags["qa_middleware_enabled"] else "")
    out = SNAPSHOT_DIR / (f"snapshot-{time.strftime('%Y%m%d-%H%M%S')}"
                          f"-deepagents-{versions.get('deepagents') or 'unknown'}{suffix}.json")
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"快照已保存: {out}")


if __name__ == "__main__":
    main()
