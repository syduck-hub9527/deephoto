"""入库观测存储:独立于业务库的轻量 SQLite(md文档/deephoto_ingestion_progress_plan.md §8.2)。

为什么独立:业务连接的写事务会跨过漫长的网络调用(描述、向量),API 连接
在其提交前看不到任何进度。观测库使用专用连接、短事务、独立提交与较短锁等待,
观测写入失败只发节流警告,绝不让原本能成功的文档入库变成失败。

注意:不能使用 db.connect 切换路径复用(它按线程缓存一组连接与路径,
混用会干扰业务连接),本模块自行管理线程本地连接。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .pipeline import progress as pg

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    instance_id   TEXT NOT NULL,
    enqueued_at   TEXT NOT NULL,
    claimed_at    TEXT,
    finished_at   TEXT,
    result        TEXT NOT NULL DEFAULT 'running',
    current_stage TEXT,
    current_item  TEXT,
    counts        TEXT,
    warnings      TEXT NOT NULL DEFAULT '[]',
    last_event_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_doc ON runs(document_id, enqueued_at DESC);

CREATE TABLE IF NOT EXISTS stages (
    run_id      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    parent      TEXT,
    seq         INTEGER NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    result      TEXT NOT NULL DEFAULT 'running',
    counts      TEXT,
    detail      TEXT,
    PRIMARY KEY (run_id, stage)
);

CREATE TABLE IF NOT EXISTS items (
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    label       TEXT,
    page        INTEGER,
    figure      TEXT,
    count       INTEGER,
    started_at  TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    result      TEXT NOT NULL DEFAULT 'running',
    error_kind  TEXT,
    detail      TEXT,
    PRIMARY KEY (run_id, kind, seq)
);
"""

_WARN_THROTTLE_SECONDS = 60.0


class ProgressStore:
    """观测读写。时钟可注入便于测试;所有写操作故障隔离。"""

    def __init__(self, db_path, *, instance_id: str = "instance",
                 now=time.time, clock=time.monotonic):
        self.db_path = Path(db_path)
        self.instance_id = instance_id
        self._now = now            # 墙钟(持久化时间戳)
        self._clock = clock        # 单调钟(单次运行内的耗时)
        self._local = threading.local()
        self._last_write_warning = 0.0
        self._init_schema()

    # ---- 连接与写保护 ----

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        try:
            self._conn().executescript(_SCHEMA)
            self._conn().commit()
        except (sqlite3.Error, OSError) as exc:
            self._write_failed("初始化观测库", exc)

    def _write_failed(self, what: str, exc: Exception) -> None:
        """观测故障只节流告警,绝不上抛打断业务。"""
        now = self._clock()
        if now - self._last_write_warning >= _WARN_THROTTLE_SECONDS:
            self._last_write_warning = now
            logger.warning("入库观测%s失败(已忽略,不影响业务): %s: %s", what, type(exc).__name__, exc)

    def _write(self, what: str, fn) -> bool:
        """短事务写入;失败回滚并节流告警,返回 False。绝不打断业务。"""
        try:
            conn = self._conn()
            fn(conn)
            conn.commit()
            return True
        except (sqlite3.Error, OSError) as exc:
            try:
                conn.rollback()
            except (sqlite3.Error, OSError, UnboundLocalError):
                pass
            self._write_failed(what, exc)
            return False

    def _iso(self) -> str:
        return pg.utc_iso(self._now())

    # ---- 任务登记 / 领取 / 中断 ----

    def register_run(self, run_id: str, document_id: str, tenant_id: str) -> None:
        """上传接口登记任务:排队阶段从此刻起算。"""
        now = self._iso()

        def op(conn):
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, document_id, tenant_id, instance_id,"
                " enqueued_at, result, warnings, last_event_at) VALUES (?,?,?,?,?,'running','[]',?)",
                (run_id, document_id, tenant_id, self.instance_id, now, now))
            conn.execute(
                "INSERT OR REPLACE INTO stages (run_id, stage, parent, seq, started_at, result)"
                " VALUES (?,?,?,0,?,'running')",
                (run_id, pg.STAGE_QUEUED, None, now))
        self._write("登记任务", op)
        logger.info("progress run registered: run=%s doc=%s", run_id, document_id)

    def latest_run_id(self, document_id: str) -> str | None:
        try:
            row = self._conn().execute(
                "SELECT run_id FROM runs WHERE document_id = ?"
                " ORDER BY enqueued_at DESC LIMIT 1", (document_id,)).fetchone()
            return row["run_id"] if row else None
        except (sqlite3.Error, OSError) as exc:
            self._write_failed("读取任务", exc)
            return None

    def claim_run(self, run_id: str) -> None:
        """后台领取:结束排队阶段(与后台处理耗时分开计)。"""
        now = self._iso()
        started = self._stage_started_at(run_id, pg.STAGE_QUEUED)

        def op(conn):
            conn.execute("UPDATE runs SET claimed_at = ?, last_event_at = ? WHERE run_id = ?",
                         (now, now, run_id))
            conn.execute(
                "UPDATE stages SET finished_at = ?, duration_ms = ?, result = ?"
                " WHERE run_id = ? AND stage = ?",
                (now, pg.elapsed_ms(started, now), pg.RESULT_SUCCEEDED, run_id, pg.STAGE_QUEUED))
        self._write("领取任务", op)

    def mark_interrupted(self) -> None:
        """启动时调用:旧实例未结束的运行标记中断(含在途单项),不伪造结束时间
        (业务终态优先于本值;未知耗时保持空,展示为未知)。"""
        interrupted = 0   # _write 只返回成功与否,真实数量经闭包带出

        def op(conn):
            nonlocal interrupted
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE result = 'running' AND instance_id != ?"
                " AND claimed_at IS NOT NULL", (self.instance_id,)).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE runs SET result = ?, current_stage = NULL, current_item = NULL"
                    " WHERE run_id = ?", (pg.RESULT_INTERRUPTED, row["run_id"]))
                conn.execute(
                    "UPDATE stages SET result = ? WHERE run_id = ? AND result = 'running'",
                    (pg.RESULT_INTERRUPTED, row["run_id"]))
                # 单项一并标记中断:否则前端逐图仍显示"进行中"并持续累计耗时
                conn.execute(
                    "UPDATE items SET result = ? WHERE run_id = ? AND result = 'running'",
                    (pg.RESULT_INTERRUPTED, row["run_id"]))
            interrupted = len(rows)
        if self._write("标记旧实例中断", op) and interrupted:
            logger.info("progress: %s 个旧实例未完成任务标记为观测中断", interrupted)

    # ---- 观察器 ----

    def observer(self, run_id: str) -> "RunObserver":
        return RunObserver(self, run_id)

    def _run_exists(self, conn, run_id: str) -> bool:
        return conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is not None

    def _stage_started_at(self, run_id: str, stage: str) -> str | None:
        try:
            row = self._conn().execute(
                "SELECT started_at FROM stages WHERE run_id = ? AND stage = ?",
                (run_id, stage)).fetchone()
            return row["started_at"] if row else None
        except (sqlite3.Error, OSError):
            return None

    # ---- 读取:列表摘要 ----

    def summaries(self, tenant_id: str, docs: list[dict]) -> dict[str, dict]:
        """每个文档的最新运行摘要(供列表接口合并)。业务终态优先:
        文档已 ready/failed 而观测仍 running 时按业务状态展示,不误报处理中。"""
        now_iso = self._iso()
        result: dict[str, dict] = {}
        for doc in docs:
            run = self._latest_run(tenant_id, doc["id"])
            if run is None:
                continue
            result[doc["id"]] = self._summary(run, doc, now_iso)
        return result

    def _latest_run(self, tenant_id: str, document_id: str):
        try:
            row = self._conn().execute(
                "SELECT * FROM runs WHERE tenant_id = ? AND document_id = ?"
                " ORDER BY enqueued_at DESC LIMIT 1", (tenant_id, document_id)).fetchone()
            return dict(row) if row else None
        except (sqlite3.Error, OSError) as exc:
            self._write_failed("读取摘要", exc)
            return None

    def _summary(self, run: dict, doc: dict, now_iso: str) -> dict:
        # 业务终态优先:观测中断/未写终态时按业务状态展示,不误报处理中
        business_terminal = doc.get("status") in ("ready", "failed")
        if business_terminal and run["result"] in (pg.RESULT_RUNNING, pg.RESULT_INTERRUPTED):
            state = "succeeded" if doc["status"] == "ready" else "failed"
        elif run["result"] == pg.RESULT_RUNNING and doc.get("status") == "queued" and not run["claimed_at"]:
            state = "queued"
        else:
            state = run["result"]
        finished = run["finished_at"] or (now_iso if state in (pg.RESULT_RUNNING, "queued") else run["last_event_at"])
        counts = _json(run["counts"]) or {}
        current = _json(run["current_item"])
        if current and current.get("started_at"):
            current = {**current, "elapsed_ms": pg.elapsed_ms(current["started_at"], now_iso)}
        stage_elapsed = None
        if run["current_stage"] and state == pg.RESULT_RUNNING:
            started = self._stage_started_at(run["run_id"], run["current_stage"])
            stage_elapsed = pg.elapsed_ms(started, now_iso)
        return {
            "run_id": run["run_id"],
            "state": state,
            "stage": run["current_stage"],
            "queue_elapsed_ms": pg.elapsed_ms(run["enqueued_at"], run["claimed_at"]) if run["claimed_at"] else None,
            "processing_elapsed_ms": pg.elapsed_ms(run["claimed_at"], finished),
            "total_elapsed_ms": pg.elapsed_ms(run["enqueued_at"], finished),
            "stage_elapsed_ms": stage_elapsed,
            "completed": counts.get("completed"),
            "total": counts.get("total"),
            "succeeded": counts.get("succeeded"),
            "failed": counts.get("failed"),
            "degraded": counts.get("degraded"),
            "current_item": current,
            "last_event_at": run["last_event_at"],
            "warnings": _json(run["warnings"]) or [],
        }

    # ---- 读取:详情 ----

    def detail(self, tenant_id: str, document_id: str,
               business_status: str | None = None) -> dict | None:
        """详情:运行摘要 + 阶段耗时 + 单项结果 + 最慢项。tenant 由路由先行校验;
        business_status 传入真实业务状态,与列表接口共用同一套终态判断。"""
        run = self._latest_run(tenant_id, document_id)
        if run is None:
            return None
        try:
            conn = self._conn()
            stages = [dict(r) for r in conn.execute(
                "SELECT * FROM stages WHERE run_id = ? ORDER BY seq, rowid", (run["run_id"],)).fetchall()]
            items = [dict(r) for r in conn.execute(
                "SELECT * FROM items WHERE run_id = ? ORDER BY kind, seq", (run["run_id"],)).fetchall()]
        except (sqlite3.Error, OSError) as exc:
            self._write_failed("读取详情", exc)
            return None
        for stage in stages:
            stage["display_name"] = pg.STAGE_NAMES.get(stage["stage"], stage["stage"])
            stage["counts"] = _json(stage["counts"])
            stage["detail"] = _json(stage["detail"])
        for item in items:
            item["detail"] = _json(item["detail"])
        finished_items = [i for i in items if i.get("duration_ms") is not None]
        slowest = sorted(finished_items, key=lambda i: i["duration_ms"], reverse=True)[:3]
        return {
            "summary": self._summary(run, {"id": document_id, "status": business_status or ""},
                                     self._iso()),
            "stage_names": dict(pg.STAGE_NAMES),
            "stages": stages,
            "items": items,
            "slowest": slowest,
        }

    # ---- 清理 ----

    def cleanup_documents(self, document_ids: list[str]) -> None:
        """删除文档时清理其观测记录;在途回调发现 run 不存在时不会重建。"""
        if not document_ids:
            return
        marks = ",".join("?" for _ in document_ids)

        def op(conn):
            run_ids = [r["run_id"] for r in conn.execute(
                f"SELECT run_id FROM runs WHERE document_id IN ({marks})", document_ids).fetchall()]
            if not run_ids:
                return
            rmarks = ",".join("?" for _ in run_ids)
            conn.execute(f"DELETE FROM stages WHERE run_id IN ({rmarks})", run_ids)
            conn.execute(f"DELETE FROM items WHERE run_id IN ({rmarks})", run_ids)
            conn.execute(f"DELETE FROM runs WHERE run_id IN ({rmarks})", run_ids)
        self._write("清理观测记录", op)


class RunObserver:
    """绑定 run_id 的观察器:阶段/单项事件写入观测库(与 NoOpObserver 同接口)。"""

    def __init__(self, store: ProgressStore, run_id: str):
        self._store = store
        self._run_id = run_id
        self._stage_clock: dict[str, float] = {}
        self._item_clock: dict[tuple[str, int], float] = {}
        self._log = logging.getLogger("deephoto.progress")

    # -- 阶段 --

    def stage_start(self, stage: str, *, parent: str | None = None,
                    total: int | None = None, detail: dict | None = None) -> None:
        store = self._store
        now = store._iso()
        self._stage_clock[stage] = store._clock()
        seq = list(pg.TOP_STAGES).index(stage) if stage in pg.TOP_STAGES else 50

        def op(conn):
            if not store._run_exists(conn, self._run_id):
                return   # 文档已删除:不重建孤立记录
            counts = json.dumps({"completed": 0, "total": total, "succeeded": 0,
                                 "failed": 0, "degraded": 0}) if total is not None else None
            conn.execute(
                "INSERT OR REPLACE INTO stages (run_id, stage, parent, seq, started_at, result, counts, detail)"
                " VALUES (?,?,?,?,?,'running',?,?)",
                (self._run_id, stage, parent, seq, now, counts,
                 json.dumps(detail, ensure_ascii=False) if detail else None))
            conn.execute(
                "UPDATE runs SET current_stage = ?, current_item = NULL, counts = ?, last_event_at = ?"
                " WHERE run_id = ?", (stage, counts, now, self._run_id))
        store._write("阶段开始", op)
        self._log.info("stage start: run=%s stage=%s total=%s", self._run_id, stage, total)

    def stage_end(self, stage: str, result: str = pg.RESULT_SUCCEEDED, *,
                  counts: dict | None = None, detail: dict | None = None,
                  duration_ms: int | None = None) -> None:
        store = self._store
        now = store._iso()
        if duration_ms is None:
            duration_ms = self._duration(self._stage_clock.pop(stage, None))
        else:
            self._stage_clock.pop(stage, None)
        started = store._stage_started_at(self._run_id, stage)

        def op(conn):
            parent = conn.execute(
                "SELECT parent FROM stages WHERE run_id = ? AND stage = ?",
                (self._run_id, stage)).fetchone()
            conn.execute(
                "UPDATE stages SET finished_at = ?, duration_ms = ?, result = ?,"
                " counts = COALESCE(?, counts), detail = COALESCE(?, detail)"
                " WHERE run_id = ? AND stage = ?",
                (now, duration_ms if duration_ms is not None else pg.elapsed_ms(started, now), result,
                 json.dumps(counts, ensure_ascii=False) if counts else None,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 self._run_id, stage))
            # 子阶段结束后把当前阶段恢复给父级(否则云端等待期间仍显示"读取与拆分")
            conn.execute("UPDATE runs SET current_stage = ?, last_event_at = ? WHERE run_id = ?",
                     (parent["parent"] if parent else None, now, self._run_id))
        store._write("阶段结束", op)
        self._log.info("stage end: run=%s stage=%s result=%s duration_ms=%s",
                       self._run_id, stage, result, duration_ms)

    # -- 单项 --

    def item_start(self, kind: str, seq: int, *, label: str | None = None,
                   page: int | None = None, figure: str | None = None) -> None:
        store = self._store
        now = store._iso()
        self._item_clock[(kind, seq)] = store._clock()
        current = json.dumps({"index": seq, "label": label, "page": page,
                              "figure_number": figure, "started_at": now}, ensure_ascii=False)

        def op(conn):
            if not store._run_exists(conn, self._run_id):
                return
            conn.execute(
                "INSERT OR REPLACE INTO items (run_id, kind, seq, label, page, figure, started_at, result)"
                " VALUES (?,?,?,?,?,?,?,'running')",
                (self._run_id, kind, seq, label, page, figure, now))
            conn.execute("UPDATE runs SET current_item = ?, last_event_at = ? WHERE run_id = ?",
                         (current, now, self._run_id))
        store._write("单项开始", op)

    def item_update(self, kind: str, seq: int, *, label: str | None = None,
                    detail: dict | None = None) -> None:
        """在途更新(如云端轮询状态):刷新摘要,不结束单项。
        当前单项保留原始开始时间——轮询只更新状态与最近事件时间,不重设计时。"""
        store = self._store
        now = store._iso()

        def op(conn):
            row = conn.execute(
                "SELECT detail, started_at FROM items WHERE run_id = ? AND kind = ? AND seq = ?",
                (self._run_id, kind, seq)).fetchone()
            if detail:
                merged = {**(_json(row["detail"] if row else None) or {}), **detail}
                conn.execute(
                    "UPDATE items SET detail = ? WHERE run_id = ? AND kind = ? AND seq = ?",
                    (json.dumps(merged, ensure_ascii=False), self._run_id, kind, seq))
            if label:
                current = json.dumps(
                    {"index": seq, "label": label,
                     "started_at": (row["started_at"] if row else None) or now},
                    ensure_ascii=False)
                conn.execute("UPDATE runs SET current_item = ?, last_event_at = ? WHERE run_id = ?",
                             (current, now, self._run_id))
            else:
                conn.execute("UPDATE runs SET last_event_at = ? WHERE run_id = ?", (now, self._run_id))
        store._write("单项更新", op)

    def item_end(self, kind: str, seq: int, result: str, *,
                 count: int | None = None, error_kind: str | None = None,
                 detail: dict | None = None) -> None:
        store = self._store
        now = store._iso()
        duration = self._duration(self._item_clock.pop((kind, seq), None))

        def op(conn):
            if not store._run_exists(conn, self._run_id):
                return   # 文档已删除:迟到回调不重建、不抛错
            detail_json = None
            if detail:
                # 与在途更新(request/upload/poll 等)合并,不能整段覆盖
                old = conn.execute(
                    "SELECT detail FROM items WHERE run_id = ? AND kind = ? AND seq = ?",
                    (self._run_id, kind, seq)).fetchone()
                merged = {**(_json(old["detail"] if old else None) or {}), **detail}
                detail_json = json.dumps(merged, ensure_ascii=False)
            conn.execute(
                "UPDATE items SET finished_at = ?, duration_ms = COALESCE(?, duration_ms), result = ?,"
                " count = COALESCE(?, count), error_kind = ?, detail = COALESCE(?, detail)"
                " WHERE run_id = ? AND kind = ? AND seq = ?",
                (now, duration, result, count, error_kind, detail_json,
                 self._run_id, kind, seq))
            counts_row = conn.execute(
                "SELECT counts FROM runs WHERE run_id = ?", (self._run_id,)).fetchone()
            counts = _bump_counts(_json(counts_row["counts"] if counts_row else None), result)
            if counts is not None:
                # 进度计数同步到在途阶段(单项都属于某个带 total 的阶段)
                conn.execute(
                    "UPDATE stages SET counts = ? WHERE run_id = ? AND result = 'running'"
                    " AND counts IS NOT NULL",
                    (json.dumps(counts), self._run_id))
            conn.execute("UPDATE runs SET counts = ?, last_event_at = ? WHERE run_id = ?",
                         (json.dumps(counts) if counts else None, now, self._run_id))
        store._write("单项结束", op)
        self._log.info("item end: run=%s kind=%s seq=%s result=%s duration_ms=%s",
                       self._run_id, kind, seq, result, duration)

    # -- 警告与终态 --

    def warn(self, message: str) -> None:
        store = self._store

        def op(conn):
            row = conn.execute(
                "SELECT warnings FROM runs WHERE run_id = ?", (self._run_id,)).fetchone()
            if row is None:
                return   # 文档已删除:迟到回调不重建、不抛错
            warnings = _json(row["warnings"]) or []
            if message not in warnings:
                warnings.append(message)
            conn.execute("UPDATE runs SET warnings = ?, last_event_at = ? WHERE run_id = ?",
                         (json.dumps(warnings, ensure_ascii=False), store._iso(), self._run_id))
        store._write("记录警告", op)

    def finish(self, result: str) -> None:
        """任务终态(业务提交成功后记录);失败时在途阶段/单项以真实耗时收尾为 failed,
        无法确定的一律不填假 0。"""
        store = self._store
        now = store._iso()

        def op(conn):
            if not store._run_exists(conn, self._run_id):
                return
            if result == pg.RESULT_FAILED:
                for stage in conn.execute(
                        "SELECT stage, started_at FROM stages WHERE run_id = ? AND result = 'running'",
                        (self._run_id,)).fetchall():
                    conn.execute(
                        "UPDATE stages SET finished_at = ?, duration_ms = ?, result = ?"
                        " WHERE run_id = ? AND stage = ?",
                        (now, pg.elapsed_ms(stage["started_at"], now), pg.RESULT_FAILED,
                         self._run_id, stage["stage"]))
                for item in conn.execute(
                        "SELECT kind, seq, started_at FROM items WHERE run_id = ? AND result = 'running'",
                        (self._run_id,)).fetchall():
                    conn.execute(
                        "UPDATE items SET finished_at = ?, duration_ms = ?, result = ?"
                        " WHERE run_id = ? AND kind = ? AND seq = ?",
                        (now, pg.elapsed_ms(item["started_at"], now), pg.RESULT_FAILED,
                         self._run_id, item["kind"], item["seq"]))
            conn.execute(
                "UPDATE runs SET result = ?, finished_at = ?, current_stage = NULL,"
                " current_item = NULL, last_event_at = ? WHERE run_id = ?",
                (result, now, now, self._run_id))
        store._write("任务终态", op)
        self._log.info("run finish: run=%s result=%s", self._run_id, result)

    # -- 内部 --

    def _duration(self, clock_start: float | None) -> int | None:
        if clock_start is None:
            return None
        return max(0, int((self._store._clock() - clock_start) * 1000))


def _json(raw) -> dict | list | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _bump_counts(counts: dict | None, result: str) -> dict | None:
    """单项结束后更新进度计数:失败/降级计入 completed,但绝不算进 succeeded。"""
    if counts is None:
        return None
    counts = dict(counts)
    counts["completed"] = counts.get("completed", 0) + 1
    if result == pg.ITEM_OK:
        counts["succeeded"] = counts.get("succeeded", 0) + 1
    elif result == pg.ITEM_DEGRADED:
        counts["degraded"] = counts.get("degraded", 0) + 1
    else:
        counts["failed"] = counts.get("failed", 0) + 1
    return counts
