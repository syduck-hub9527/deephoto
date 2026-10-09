"""03:SqliteSaver 保存图状态,SqliteStore 保存会话归属/范围/已验证引用。

两者不是一个事务:先标记 running,只有图执行与答案组装均完成才置 ready。
失败/中断的会话拒绝继续,避免把半轮 checkpoint 当作成功历史重新执行。
同会话互斥仅覆盖本进程;本阶段面向项目现有单进程本地部署。
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.store.sqlite import SqliteStore

SESSION_RE = re.compile(r"sess_[0-9a-f]{32}\Z")
EVIDENCE_LIMIT = 2048


class SessionError(ValueError):
    """会话不存在、范围不匹配或上轮未完成。"""


class DocumentScopeError(SessionError):
    """文档暂不可用,图尚未执行,不改变健康会话状态。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope(ctx) -> str:
    raw = json.dumps([ctx.tenant_id, ctx.user_id], ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _validate_id(session_id: str) -> None:
    if not SESSION_RE.fullmatch(session_id):
        raise SessionError("会话 ID 格式无效")


def _recent_ids(previous, current) -> list[str]:
    # 最近一次引用排到末尾,超额时淘汰最久未引用的 ID。
    order = dict.fromkeys(previous)
    for value in current:
        order.pop(value, None)
        order[value] = None
    return list(order)[-EVIDENCE_LIMIT:]


@dataclass
class SessionTurn:
    runtime: "QAPersistence"
    namespace: tuple[str, ...]
    session_id: str
    thread_id: str
    metadata: dict
    completed: bool = False

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}}

    def complete(self, result: dict) -> None:
        if result.get("answer") is None:
            return
        metadata = dict(self.metadata)
        metadata.update(
            status="ready", updated_at=_now(),
            chunk_ids=_recent_ids(metadata.get("chunk_ids", []),
                                  [c["chunk_id"] for c in result["citations"]]),
            image_ids=_recent_ids(metadata.get("image_ids", []),
                                  [i["image_occurrence_id"] for i in result["images"]]),
        )
        self.runtime.store.put(self.namespace, self.session_id, metadata)
        self.metadata = metadata
        self.completed = True


class QAPersistence:
    def __init__(self, checkpoint_path: Path, store_path: Path):
        self._checkpoint_path = checkpoint_path
        self._store_path = store_path
        self._lock = Lock()
        self._active: set[str] = set()
        self._stack: ExitStack | None = None
        self._closed = False
        self.saver: SqliteSaver | None = None
        self.store: SqliteStore | None = None

    def _open(self) -> None:
        # 调用者持 _lock;不使用业务 db.connect 的线程亲和连接。
        if self._closed:
            raise RuntimeError("问答持久化服务已关闭")
        if self._stack is not None:
            return
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.store.sqlite import SqliteStore
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        stack = ExitStack()
        try:
            saver = stack.enter_context(SqliteSaver.from_conn_string(str(self._checkpoint_path)))
            store = stack.enter_context(SqliteStore.from_conn_string(str(self._store_path)))
            # 连接在首次请求之前设置;运行中仍由框架各自的锁保护访问。
            saver.conn.execute("PRAGMA busy_timeout=30000")
            store.conn.execute("PRAGMA busy_timeout=30000")
            store.conn.execute("PRAGMA journal_mode=WAL")
            store.setup()
        except BaseException:
            stack.close()
            raise
        self.saver, self.store, self._stack = saver, store, stack

    @contextmanager
    def _lease(self, thread_id: str):
        with self._lock:
            self._open()
            if thread_id in self._active:
                raise RuntimeError("该会话正在回答,请等待本轮结束")
            self._active.add(thread_id)
        try:
            yield
        finally:
            with self._lock:
                self._active.remove(thread_id)

    @staticmethod
    def _keys(ctx, session_id: str):
        owner = _scope(ctx)
        namespace = ("deephoto", "qa_sessions", owner)
        return namespace, f"qa:{owner}:{session_id}"

    @contextmanager
    def turn(self, ctx, session_id: str | None, document_id: str | None, profile: dict,
             *, preflight=None):
        existing = session_id is not None
        session_id = session_id or f"sess_{uuid4().hex}"
        _validate_id(session_id)
        namespace, thread_id = self._keys(ctx, session_id)
        with self._lease(thread_id):
            item = self.store.get(namespace, session_id)
            if existing and item is None:
                raise SessionError("会话不存在或无权访问,请开启新会话")
            if item is not None:
                metadata = dict(item.value)
                if metadata.get("document_id") != document_id:
                    raise SessionError("文档范围已改变,请开启新会话")
                if metadata.get("profile") != profile:
                    raise SessionError("问答配置已改变,请开启新会话")
                if metadata.get("status") != "ready":
                    raise SessionError("上一轮会话未完成,请开启新会话")
                if self.saver.get_tuple({"configurable": {"thread_id": thread_id}}) is None:
                    raise SessionError("会话状态缺失,请开启新会话")
            else:
                metadata = {"version": 1, "document_id": document_id, "profile": profile,
                            "created_at": _now(), "chunk_ids": [], "image_ids": []}
            # 归属/scope/profile/status 校验后,但 running 写入前进行业务预检。
            # 预检异常不进入 turn 的失败 finally,旧 metadata/checkpoint 均保持原样。
            if preflight is not None:
                preflight()
            metadata.update(status="running", updated_at=_now())
            self.store.put(namespace, session_id, metadata)
            turn = SessionTurn(self, namespace, session_id, thread_id, metadata)
            try:
                yield turn
            finally:
                if not turn.completed:
                    failed = dict(turn.metadata, status="failed", updated_at=_now())
                    self.store.put(namespace, session_id, failed)

    def delete(self, ctx, session_id: str) -> None:
        _validate_id(session_id)
        namespace, thread_id = self._keys(ctx, session_id)
        with self._lease(thread_id):
            item = self.store.get(namespace, session_id)
            if item is None:
                raise SessionError("会话不存在或无权访问")
            # 先删除图状态;若第二步失败,后续 turn 会因状态缺失拒绝恢复。
            self.saver.delete_thread(thread_id)
            self.store.delete(namespace, session_id)

    @contextmanager
    def storage(self):
        # 05 的短 Store 操作也占用资源租约,避免 close 在访问过程中关闭连接。
        with self._lease(f"storage:{uuid4().hex}"):
            yield self.store

    def close(self) -> None:
        with self._lock:
            if self._active:
                raise RuntimeError("仍有问答运行,不能关闭持久化连接")
            self._closed = True
            if self._stack is not None:
                self._stack.close()
                self._stack = None
