"""03:SqliteSaver 保存图状态,SqliteStore 保存会话归属/范围/已验证引用。

两者不是一个事务:先标记 running,只有图执行与答案组装均完成才置 ready。
失败/中断的会话拒绝继续,避免把半轮 checkpoint 当作成功历史重新执行。
同会话互斥仅覆盖本进程;本阶段面向项目现有单进程本地部署。

06 HITL 增补状态 awaiting_approval:图在审批闸门挂起时 park(不写 failed),
审批入口经 allow_awaiting 恢复;超时由下一次请求惰性触发自动拒绝(5.5),
不设后台任务,与单进程约束一致。
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.store.sqlite import SqliteStore

SESSION_RE = re.compile(r"sess_[0-9a-f]{32}\Z")
EVIDENCE_LIMIT = 2048

# 06:挂起元数据特有的键;complete/restore 写 ready 时必须剥掉,不带到下一轮
_PARK_KEYS = ("approvals", "decided", "expires_at", "previous")


class SessionError(ValueError):
    """会话不存在、范围不匹配或上轮未完成。"""


class DocumentScopeError(SessionError):
    """文档暂不可用,图尚未执行,不改变健康会话状态。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def deadline(seconds: int) -> str:
    """挂起截止时刻(06);超时判定用 parked_expired,不设后台任务。"""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def parked_expired(metadata: dict) -> bool:
    """挂起是否已过 expires_at;键缺失或格式损坏视为未过期(交由状态机其他分支处理)。"""
    expires = metadata.get("expires_at")
    if not expires:
        return False
    try:
        return datetime.now(timezone.utc) > datetime.fromisoformat(str(expires))
    except ValueError:
        return False


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
    # 图是否已开始执行。未开始(构建智能体失败等)时没有写过任何 checkpoint,
    # 会话应原样保留;开始后才可能留下半轮 checkpoint,此时才判 failed。
    started: bool = False
    previous: dict | None = None      # 进入本轮前的元数据;新会话为 None
    # 06:park_previous 是挂起超时后的恢复落点(进入挂起轮之前的 ready 元数据;
    # 新会话为首轮的等价 ready 形态)。parked=True 表示本轮停在审批闸门。
    park_previous: dict | None = None
    parked: bool = False

    def mark_started(self) -> None:
        self.started = True

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}}

    def park(self, approvals: list, decided: dict, expires_at: str) -> None:
        """本轮在审批闸门挂起:立即写 awaiting_approval 元数据(不判 failed)。

        立即落盘而不是等 finally:approval_required 事件发出后,客户端可能马上
        调审批接口,那时状态必须已可读。恢复所需的 approvals/decided/expires_at
        与超时恢复落点 previous 一并随元数据保存。
        """
        self.parked = True
        awaiting = dict(self.metadata)
        awaiting.update(status="awaiting_approval", updated_at=_now(),
                        approvals=approvals, decided=decided, expires_at=expires_at,
                        previous=self.park_previous)
        self.runtime.store.put(self.namespace, self.session_id, awaiting)
        self.metadata = awaiting

    def restore(self, metadata: dict) -> None:
        """放弃本轮输出,把会话恢复到给定元数据(06 惰性超时:自动拒绝跑完后恢复 ready)。"""
        restored = {k: v for k, v in metadata.items() if k not in _PARK_KEYS}
        restored.update(status="ready", updated_at=_now())
        self.runtime.store.put(self.namespace, self.session_id, restored)
        self.metadata = restored
        self.completed = True     # 恢复即本轮终点,finally 不再写 failed

    def complete(self, result: dict) -> None:
        answer = result.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            # 06 R3:空答案不写 ready,交由 finally 按 started 判定(原语义:None 才不写,
            # 收紧为空白也不写;流式路径的空答案本就走 error 事件,不受影响)。
            return
        metadata = dict(self.metadata)
        for key in _PARK_KEYS:
            metadata.pop(key, None)
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
             *, preflight=None, allow_awaiting: bool = False):
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
                # 06:awaiting_approval 只允许审批入口(allow_awaiting)与惰性超时恢复进入
                allowed = {"ready", "awaiting_approval"} if allow_awaiting else {"ready"}
                if metadata.get("status") not in allowed:
                    if metadata.get("status") == "awaiting_approval":
                        raise SessionError("会话正在等待图片审批,请先完成审批或开启新会话")
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
            previous = dict(item.value) if item is not None else None
            # 挂起的超时恢复落点:已有会话指回进入本轮前的元数据;审批恢复轮再挂起时
            # 指回最初的 ready(而不是上一段 awaiting);新会话用首轮的等价 ready 形态。
            if previous is not None and previous.get("status") == "awaiting_approval":
                park_previous = previous.get("previous") or previous
            elif previous is not None:
                park_previous = previous
            else:
                park_previous = {**metadata, "status": "ready"}
            metadata.update(status="running", updated_at=_now())
            self.store.put(namespace, session_id, metadata)
            turn = SessionTurn(self, namespace, session_id, thread_id, metadata,
                               previous=previous, park_previous=park_previous)
            try:
                yield turn
            finally:
                if turn.parked:
                    pass          # park() 已落盘 awaiting_approval,不是失败
                elif not turn.completed:
                    if previous is not None and not turn.started:
                        # 已有会话、图未运行(构建智能体失败等):没有写过 checkpoint,原样恢复,
                        # 不能因为一次与会话本身无关的错误判死一个健康会话。
                        self.store.put(namespace, session_id, previous)
                    else:
                        # 图已开始(可能留下半轮 checkpoint),或这是新会话(客户端已拿到其 ID,
                        # 保留 failed 占位让下次请求得到明确的"请开启新会话"):判 failed。
                        failed = dict(turn.metadata, status="failed", updated_at=_now())
                        self.store.put(namespace, session_id, failed)

    def inspect(self, ctx, session_id: str) -> dict | None:
        """读取会话元数据快照(不进入租约、不校验状态机);不存在返回 None。

        供 409 待审批探测与惰性超时判断;它只读,不替代 turn() 的归属/状态校验。
        """
        _validate_id(session_id)
        namespace, _ = self._keys(ctx, session_id)
        with self._lock:
            self._open()
            item = self.store.get(namespace, session_id)
        return dict(item.value) if item is not None else None

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
