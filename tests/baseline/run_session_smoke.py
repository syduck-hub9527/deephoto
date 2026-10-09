"""03 多轮会话真实冒烟(md文档 03 §7.5/§10):连续 session 问答,不重用浏览器 history。

run_snapshot.py 每题都是新会话,覆盖不了多轮恢复;本脚本用固定轮次验证:
- 续问不传浏览器 history,模型仍理解指代(服务端 checkpoint 恢复了历史);
- 首轮后端认可的引用在续轮仍被接受(Store 只存已验证 ID,续轮重新校验);
- 结束后显式删除会话,Store 元数据与 checkpoint 都被清除。

用法(需要 03 开关,数据目录建议指向副本):
  DEEPHOTO_DATA_DIR=/tmp/deephoto-smoke DEEPHOTO_QA_PERSISTENCE_ENABLED=true \
      .venv/bin/python tests/baseline/run_session_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_snapshot import _build_qa  # noqa: E402  复用与 app 一致的接线

from deephoto.config import load_settings  # noqa: E402
from deephoto.db import connect  # noqa: E402
from deephoto.security import LOCAL_CTX  # noqa: E402

TURNS = [
    "这本书的第 4 章讲的是哪种工艺？先用一句话告诉我。",
    "它主要有哪几种方式？各有什么特点？",          # 指代题:不带 history,靠 checkpoint 恢复上下文
    "把和它相关的示意图给我看一张。",                # 图片追问:检验续轮的图片引用链
]

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def main() -> None:
    settings = load_settings()
    if not settings.qa_persistence_enabled:
        sys.exit("需要 DEEPHOTO_QA_PERSISTENCE_ENABLED=true(且不要带浏览器 history 语义)")
    qa, _knowledge = _build_qa(settings)
    conn = connect(settings.db_path)

    session_id = None
    records = []
    try:
        for i, question in enumerate(TURNS, 1):
            started = time.monotonic()
            # 关键:续轮不传 history;首轮之外的上下文只能来自服务端 checkpoint
            result = qa.answer(conn, LOCAL_CTX, question, session_id=session_id)
            elapsed = round(time.monotonic() - started, 1)
            if session_id is None:
                session_id = result.get("session_id")
                print(f"会话已建立: {session_id}", flush=True)
            elif result.get("session_id") != session_id:
                print(f"!! 第 {i} 轮返回的 session_id 变了: {result.get('session_id')}", flush=True)
            records.append({
                "turn": i, "question": question, "elapsed_s": elapsed,
                "answer": result.get("answer"), "error": result.get("error"),
                "citations": [c["chunk_id"] for c in result.get("citations", [])],
                "images": [im["image_occurrence_id"] for im in result.get("images", [])],
            })
            print(f"[{i}/{len(TURNS)}] {elapsed}s, {len(records[-1]['citations'])} 条引用, "
                  f"{len(records[-1]['images'])} 张图"
                  + (f", 错误: {records[-1]['error']}" if records[-1]["error"] else ""), flush=True)
            print("    " + (result.get("answer") or "")[:150].replace("\n", " "), flush=True)
    finally:
        # 删除会话:Store 元数据与 checkpoint 都应清除;随后 qa.close 释放框架连接
        try:
            if session_id:
                qa.delete_session(LOCAL_CTX, session_id)
                print(f"会话已删除: {session_id}", flush=True)
        finally:
            qa.close()

    SNAPSHOT_DIR.mkdir(exist_ok=True)
    out = SNAPSHOT_DIR / f"session-smoke-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"meta": {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                        "chat_model": settings.chat_model,
                                        "qa_skills_enabled": settings.qa_skills_enabled,
                                        "qa_memory_enabled": settings.qa_memory_enabled},
                               "turns": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"冒烟记录已保存: {out}")


if __name__ == "__main__":
    main()
