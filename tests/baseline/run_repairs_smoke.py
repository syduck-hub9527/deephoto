"""fix-qa-before-06 真实冒烟:图像载荷不重放 + 预检不判死会话。

对应文档 §7.3 的灰度项 1/2(修复 1 与修复 2 的真实链路验证):
  T1(限定文档):看图问题,inspect_image 返回真实 base64 载荷进 checkpoint
  T2(同会话,不带 history):文本指代续问 -> 验证续轮不重放载荷且回答正常
  P1:把文档置 queued -> SSE 续问应返回 document_scope_unavailable,会话不被判死
  P2:恢复 ready -> 同 session_id 续问成功
修复 3(04 关闭默认 GP)由离线测试固定工具可见性,真实链路不重复验证。

用法(建议数据目录指向副本,脚本会临时改动文档状态):
  DEEPHOTO_DATA_DIR=/tmp/deephoto-smoke DEEPHOTO_QA_PERSISTENCE_ENABLED=true \
      .venv/bin/python tests/baseline/run_repairs_smoke.py <document_id>
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_snapshot import _build_qa  # noqa: E402

from deephoto import repo  # noqa: E402
from deephoto.config import load_settings  # noqa: E402
from deephoto.db import connect  # noqa: E402
from deephoto.security import LOCAL_CTX  # noqa: E402

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def main() -> None:
    settings = load_settings()
    if not settings.qa_persistence_enabled:
        sys.exit("需要 DEEPHOTO_QA_PERSISTENCE_ENABLED=true")
    document_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not document_id:
        sys.exit("用法: run_repairs_smoke.py <document_id>(限定文档会话,预检场景需要)")
    qa, _knowledge = _build_qa(settings)
    conn = connect(settings.db_path)
    records: list[dict] = []
    session_id = None

    def record(step: str, **fields):
        records.append({"step": step, **fields})
        print(f"[{step}] " + " ".join(f"{k}={v}" for k, v in fields.items() if k != "answer"), flush=True)

    try:
        # T1:真实看图,载荷进 checkpoint
        started = time.monotonic()
        r1 = qa.answer(conn, LOCAL_CTX,
                       "图 4.1 氧化期间的氧化剂流动示意图中,氧化剂从主气流到硅片表面要经过哪几个阶段?",
                       document_id=document_id)
        session_id = r1.get("session_id")
        record("T1-看图", elapsed_s=round(time.monotonic() - started, 1),
               session_id=session_id, images=len(r1.get("images", [])),
               citations=len(r1.get("citations", [])), answer=(r1.get("answer") or "")[:120])

        # T2:同会话文本指代续问(修复 1:载荷不重放,上下文仍在)
        started = time.monotonic()
        r2 = qa.answer(conn, LOCAL_CTX,
                       "这个机理对应的模型叫什么名字？它把氧化生长过程分成哪两个阶段？",
                       document_id=document_id, session_id=session_id)
        record("T2-指代续问", elapsed_s=round(time.monotonic() - started, 1),
               citations=len(r2.get("citations", [])), answer=(r2.get("answer") or "")[:120])

        # P1:文档转 queued -> 预检应返回可恢复错误且会话不死(修复 2)
        repo.update_document_status(conn, document_id, "queued")
        events = list(qa.answer_stream(LOCAL_CTX, "干氧氧化和湿氧氧化哪个速率更快？",
                                       document_id=document_id, session_id=session_id))
        record("P1-预检拦截", events=json.dumps(events, ensure_ascii=False),
               recoverable=all(e.get("code") == "document_scope_unavailable" for e in events))

        # P2:恢复 ready -> 同 ID 续问成功
        repo.update_document_status(conn, document_id, "ready")
        started = time.monotonic()
        r3 = qa.answer(conn, LOCAL_CTX, "干氧氧化和湿氧氧化哪个速率更快？",
                       document_id=document_id, session_id=session_id)
        record("P2-恢复续问", elapsed_s=round(time.monotonic() - started, 1),
               same_session=r3.get("session_id") == session_id,
               citations=len(r3.get("citations", [])), answer=(r3.get("answer") or "")[:120])
    finally:
        try:
            # 脚本改过文档状态,无论成功失败都恢复 ready;会话记录删除
            repo.update_document_status(conn, document_id, "ready")
            if session_id:
                qa.delete_session(LOCAL_CTX, session_id)
                print(f"会话已删除: {session_id}", flush=True)
        finally:
            qa.close()

    SNAPSHOT_DIR.mkdir(exist_ok=True)
    out = SNAPSHOT_DIR / f"repairs-smoke-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"meta": {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                        "chat_model": settings.chat_model},
                               "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"冒烟记录已保存: {out}")


if __name__ == "__main__":
    main()
