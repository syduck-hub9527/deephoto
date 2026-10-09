"""05 Skills/Memory 真实冒烟(md文档 05 §10):偏好编排 + 技能加载观测。

固定编排(对应文档 §10 的 Memory 评估步骤;技能加载用 spy 记录):
  0. PUT 偏好 en+concise(服务级 PreferenceMemory,不走 HTTP)
  1. 新会话问第 4 章工艺   -> 偏好应体现在表达(英文/简洁)
  2. 同 session 改 zh+detailed 后续问 -> FreshMemoryMiddleware 应让本轮用新偏好
  3. DELETE 偏好后同 session 再问   -> 不再注入偏好
全程记录 ReadOnlySnapshot 的 read/download,确认 /skills/ 与 /memory/ 的实际读取。
结束后删除会话并关闭资源。真实模型的语言/详略遵从度以记录为准,供人工评估。

用法:
  DEEPHOTO_DATA_DIR=/tmp/deephoto-smoke DEEPHOTO_QA_SKILLS_ENABLED=true \
  DEEPHOTO_QA_MEMORY_ENABLED=true DEEPHOTO_QA_PERSISTENCE_ENABLED=true \
      .venv/bin/python tests/baseline/run_context_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_snapshot import _build_qa  # noqa: E402  与 app 一致的接线

from deephoto.config import load_settings  # noqa: E402
from deephoto.db import connect  # noqa: E402
from deephoto.security import LOCAL_CTX  # noqa: E402

QUESTIONS = [
    "这本书的第 4 章讲的是哪种工艺？",        # 偏好 en+concise 生效中
    "它有哪几种主要方式？分别适合什么场景？",  # 已在同 session 改为 zh+detailed
    "用一句话总结它的核心机理。",              # 偏好已删除,应回到默认(中文)
]

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def _lang_profile(text: str) -> dict:
    """粗粒度语言画像:统计拉丁字母与 CJK 字数,供人工判断偏好是否被遵从。"""
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return {"latin_chars": latin, "cjk_chars": cjk}


def _spy_on_snapshot(log: list) -> None:
    """记录模型/框架对只读快照的实际读取(技能渐进披露 + 偏好加载)。"""
    from deephoto.agent.context import ReadOnlySnapshot

    orig_read = ReadOnlySnapshot.read
    orig_download = ReadOnlySnapshot.download_files

    def read(self, file_path, offset=0, limit=2000):
        log.append({"op": "read", "path": file_path})
        return orig_read(self, file_path, offset, limit)

    def download_files(self, paths):
        log.append({"op": "download", "paths": list(paths)})
        return orig_download(self, paths)

    ReadOnlySnapshot.read = read
    ReadOnlySnapshot.download_files = download_files


def main() -> None:
    settings = load_settings()
    for flag in ("qa_skills_enabled", "qa_memory_enabled", "qa_persistence_enabled"):
        if not getattr(settings, flag):
            sys.exit(f"需要 DEEPHOTO_{flag.upper()}=true")
    qa, _knowledge = _build_qa(settings)
    conn = connect(settings.db_path)
    fs_log: list = []
    _spy_on_snapshot(fs_log)

    from deephoto.agent.context import Preferences
    memory = qa.preference_memory()

    session_id = None
    records = []
    try:
        memory.put(LOCAL_CTX, Preferences(language="en", detail="concise"))
        print("偏好已写入: en + concise", flush=True)
        for i, question in enumerate(QUESTIONS, 1):
            if i == 2:
                memory.put(LOCAL_CTX, Preferences(language="zh", detail="detailed"))
                print("偏好已更新: zh + detailed(同 session 续问)", flush=True)
            if i == 3:
                memory.delete(LOCAL_CTX)
                print("偏好已删除(同 session 续问)", flush=True)
            fs_log.clear()
            started = time.monotonic()
            result = qa.answer(conn, LOCAL_CTX, question, session_id=session_id)
            elapsed = round(time.monotonic() - started, 1)
            if session_id is None:
                session_id = result.get("session_id")
                print(f"会话已建立: {session_id}", flush=True)
            answer = result.get("answer") or ""
            records.append({
                "turn": i, "question": question, "elapsed_s": elapsed,
                "answer": answer, "error": result.get("error"),
                "language_profile": _lang_profile(answer),
                "fs_reads": list(fs_log),
                "citations": [c["chunk_id"] for c in result.get("citations", [])],
                "images": [im["image_occurrence_id"] for im in result.get("images", [])],
            })
            profile = records[-1]["language_profile"]
            print(f"[{i}/3] {elapsed}s, 引用 {len(records[-1]['citations'])}, "
                  f"图 {len(records[-1]['images'])}, 字符 拉丁={profile['latin_chars']} CJK={profile['cjk_chars']}",
                  flush=True)
            print("    " + answer[:120].replace("\n", " "), flush=True)
            skill_reads = [e for e in fs_log
                           if "SKILL.md" in str(e.get("path") or e.get("paths"))]
            memory_reads = [e for e in fs_log if "AGENTS.md" in str(e.get("path") or e.get("paths"))]
            # 路径已被 Composite 剥掉路由前缀:/skills/main/... 到这里是 /main/...
            print(f"    技能文件操作 {len(skill_reads)} 次(含框架索引),偏好文件操作 {len(memory_reads)} 次",
                  flush=True)
    finally:
        try:
            memory.delete(LOCAL_CTX)
            if session_id:
                qa.delete_session(LOCAL_CTX, session_id)
                print(f"会话与偏好已删除: {session_id}", flush=True)
        finally:
            qa.close()

    SNAPSHOT_DIR.mkdir(exist_ok=True)
    out = SNAPSHOT_DIR / f"context-smoke-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"meta": {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                        "chat_model": settings.chat_model},
                               "turns": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"冒烟记录已保存: {out}")


if __name__ == "__main__":
    main()
