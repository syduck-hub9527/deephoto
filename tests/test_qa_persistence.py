"""03:真实 SQLite / LangGraph 持久化,离线模型,不访问付费 API。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel
from test_kb_vfs import _Base

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.tools import ToolRuntime, tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from deephoto import repo
from deephoto.agent.persistence import EVIDENCE_LIMIT, QAPersistence, SessionError, _recent_ids
from deephoto.agent.qa import QAService
from deephoto.config import load_settings
from deephoto.security import AuthContext


class FrameworkPersistenceTest(unittest.TestCase):
    def test_run_limit_resets_and_store_survives_thread_delete(self):
        seen = []
        @tool
        def probe(runtime: ToolRuntime) -> str:
            """探针:证明子智能体取得 Store。"""
            seen.append(type(runtime.store).__name__)
            runtime.store.put(("probe",), "answer", {"value": 42})
            return "ok"
        with SqliteSaver.from_conn_string(":memory:") as saver, SqliteStore.from_conn_string(":memory:") as store:
            store.setup()
            model = ScriptedFakeChatModel(script=[
                {"tool": "task", "args": {"subagent_type": "worker", "description": "task1"}},
                {"tool": "probe"}, "brief1", "answer1",
                {"tool": "task", "args": {"subagent_type": "worker", "description": "task2"}},
                {"tool": "probe"}, "brief2", "answer2"])
            def build():
                return create_deep_agent(model=model, checkpointer=saver, store=store,
                                         middleware=[ModelCallLimitMiddleware(run_limit=2)],
                                         subagents=[{"name": "worker", "description": "probe", "tools": [probe],
                                                     "system_prompt": "sub"}])
            config = {"configurable": {"thread_id": "t"}}
            build().invoke({"messages": [{"role": "user", "content": "q1"}]}, config=config)
            result = build().invoke({"messages": [{"role": "user", "content": "q2"}]}, config=config)
            self.assertEqual(result["messages"][-1].content, "answer2")
            self.assertEqual(model.calls_made, 8)  # 两轮主级各 2 次,子级各 2 次
            self.assertEqual(seen, ["SqliteStore", "SqliteStore"])
            self.assertEqual([m.content for m in model.requests[5]], ["sub", "task2"])
            namespaces = {row[0] for row in saver.conn.execute("SELECT checkpoint_ns FROM checkpoints")}
            self.assertIn("", namespaces)
            self.assertGreater(len(namespaces), 1)  # 子图也保存了内部状态
            saver.delete_thread("t")
            self.assertEqual(list(saver.list(config)), [])
            self.assertEqual(store.get(("probe",), "answer").value, {"value": 42})

    def test_checkpoint_recovers_after_closing_and_reopening_connections(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "cp.db")
            config = {"configurable": {"thread_id": "t"}}
            with SqliteSaver.from_conn_string(path) as saver:
                create_deep_agent(model=ScriptedFakeChatModel(script=["a1"]), checkpointer=saver).invoke(
                    {"messages": [{"role": "user", "content": "q1"}]}, config=config)
            with SqliteSaver.from_conn_string(path) as saver:
                model = ScriptedFakeChatModel(script=["a2"])
                create_deep_agent(model=model, checkpointer=saver).invoke(
                    {"messages": [{"role": "user", "content": "q2"}]}, config=config)
                self.assertIn("q1", [m.content for m in model.requests[0]])
                self.assertIn("a1", [m.content for m in model.requests[0]])

    def test_evidence_retention_is_bounded_and_recently_referenced_ids_win(self):
        previous = [f"c{i}" for i in range(EVIDENCE_LIMIT)]
        result = _recent_ids(previous, ["c0", "new"])
        self.assertEqual(len(result), EVIDENCE_LIMIT)
        self.assertNotIn("c1", result)
        self.assertEqual(result[-2:], ["c0", "new"])


class ServicePersistenceTest(_Base):
    def setUp(self):
        super().setUp()
        self.services = []

    def tearDown(self):
        for qa in self.services:
            qa.close()
        super().tearDown()

    def _qa(self, *, enabled=True, delegating=False, kb=False, middleware=True):
        knowledge = self._vfs()._knowledge
        knowledge.search = lambda *args: {"chunks": [{"chunk_id": self.c1, "text": "正文"}], "images": []}
        settings = SimpleNamespace(db_path=self.db_path, qa_persistence_enabled=enabled,
                                   qa_subagents_enabled=delegating, qa_kb_vfs_enabled=kb,
                                   qa_middleware_enabled=middleware, qa_main_max_model_calls=3,
                                   qa_main_recursion_limit=60,
                                   chat_model=f"fake-03-{int(delegating)}-{int(kb)}")
        qa = QAService(settings, knowledge)
        self.services.append(qa)
        return qa

    def _model(self, qa, script, **kwargs):
        qa._chat_model = ScriptedFakeChatModel(script=script, model_name=qa.settings.chat_model,
                                              ls_provider="openai", **kwargs)
        return qa._chat_model

    def _first(self, qa):
        self._model(qa, [{"tool": "search_knowledge", "args": {"query": "x"}},
                         f"answer [chunk:{self.c1}] [chunk:fake]"])
        return qa.answer(self.conn, self.ctx, "q1")

    def _metadata(self, qa, session_id):
        ns, _ = qa._sessions()._keys(self.ctx, session_id)
        return qa._sessions().store.get(ns, session_id).value

    def test_disabled_preserves_output_history_and_no_persistence_files(self):
        qa = self._qa(enabled=False, middleware=False)
        model = self._model(qa, ["answer"])
        result = qa.answer(self.conn, self.ctx, "q", history=[{"role": "user", "content": "old"}])
        self.assertEqual(set(result), {"answer", "citations", "images"})
        self.assertIn("old", [m.content for m in model.requests[0]])
        self.assertIsNone(qa._session_runtime)
        self.assertFalse(self.db_path.with_name("qa-checkpoints.db").exists())

    def test_initial_session_does_not_import_browser_history_or_trust_its_citations(self):
        qa = self._qa()
        model = self._model(qa, [f"answer [chunk:{self.c1}]"])
        result = qa.answer(self.conn, self.ctx, "q", history=[
            {"role": "user", "content": "UNTRUSTED_HISTORY"},
            {"role": "assistant", "content": f"fake [chunk:{self.c1}]"}])
        self.assertNotIn("UNTRUSTED_HISTORY", str(model.requests[0]))
        self.assertEqual(result["citations"], [])
        self.assertEqual(self._metadata(qa, result["session_id"])["chunk_ids"], [])

    def test_verified_citations_survive_second_turn_without_new_tools(self):
        qa = self._qa()
        first = self._first(qa)
        model = self._model(qa, [f"follow-up [chunk:{self.c1}] [chunk:fake]"])
        result = qa.answer(self.conn, self.ctx, "q2", session_id=first["session_id"],
                           history=[{"role": "user", "content": "DO_NOT_APPEND"}])
        self.assertEqual(result["session_id"], first["session_id"])
        self.assertEqual([c["chunk_id"] for c in result["citations"]], [self.c1])
        self.assertEqual(result["tool_trace"]["started"], 0)
        contents = [m.content for m in model.requests[0]]
        self.assertEqual(contents.count("q1"), 1)
        self.assertEqual(contents.count("q2"), 1)
        self.assertNotIn("DO_NOT_APPEND", contents)

    def test_restart_restores_history_and_verified_evidence(self):
        qa = self._qa()
        first = self._first(qa)
        qa.close()
        restarted = self._qa()
        model = self._model(restarted, [f"continued [chunk:{self.c1}]"])
        result = restarted.answer(self.conn, self.ctx, "q2", session_id=first["session_id"])
        self.assertEqual(result["citations"][0]["chunk_id"], self.c1)
        self.assertIn("q1", [m.content for m in model.requests[0]])

    def test_new_sessions_are_isolated(self):
        qa = self._qa()
        first = self._first(qa)
        model = self._model(qa, [f"new [chunk:{self.c1}]"])
        second = qa.answer(self.conn, self.ctx, "q_new")
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(second["citations"], [])
        self.assertNotIn("q1", [m.content for m in model.requests[0]])

    def test_same_session_cannot_be_used_by_another_tenant_or_user(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        for other in [self.ctx.__class__("tb", "u"), self.ctx.__class__("ta", "other-user")]:
            with self.subTest(ctx=other), self.assertRaisesRegex(SessionError, "不存在或无权"):
                qa.answer(self.conn, other, "q", session_id=session_id)
        self.assertEqual(self._metadata(qa, session_id)["status"], "ready")

    def test_scope_and_profile_changes_require_new_session(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        with self.assertRaisesRegex(SessionError, "文档范围"):
            qa.answer(self.conn, self.ctx, "q", document_id=self.doc_a, session_id=session_id)
        qa.settings.qa_middleware_enabled = False
        with self.assertRaisesRegex(SessionError, "问答配置"):
            qa.answer(self.conn, self.ctx, "q", session_id=session_id)

    def test_invalid_and_unknown_session_ids_do_not_create_sessions(self):
        qa = self._qa()
        for session_id in ["arbitrary-thread", "sess_" + "0" * 32]:
            with self.subTest(session_id=session_id), self.assertRaises(SessionError):
                qa.answer(self.conn, self.ctx, "q", session_id=session_id)
        namespace, _ = qa._sessions()._keys(self.ctx, "unused")
        self.assertEqual(qa._sessions().store.search(namespace), [])

    def test_prior_evidence_is_rechecked_for_ready_status(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        repo.update_document_status(self.conn, self.doc_a, "failed")
        self.conn.commit()
        self._model(qa, [f"old evidence [chunk:{self.c1}]"])
        result = qa.answer(self.conn, self.ctx, "q2", session_id=session_id)
        self.assertEqual(result["citations"], [])

    def test_long_evidence_list_respects_sqlite_variable_limit(self):
        qa = self._qa()
        metadata = {"document_id": None, "chunk_ids": [f"missing{i}" for i in range(1500)] + [self.c1],
                    "image_ids": []}
        previous = self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        try:
            chunks, images = qa._session_cited_ids(self.conn, self.ctx, SimpleNamespace(metadata=metadata))
        finally:
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
        self.assertEqual(chunks, {self.c1})
        self.assertEqual(images, set())

    def test_image_references_are_verified_and_carried(self):
        qa = self._qa()
        qa.knowledge.search = lambda *args: {"chunks": [], "images": [{"image_occurrence_id": self.occ}]}
        self._model(qa, [{"tool": "search_knowledge", "args": {"query": "图"}}, f"[image:{self.occ}]"])
        first = qa.answer(self.conn, self.ctx, "图")
        self._model(qa, [f"[image:{self.occ}]"])
        second = qa.answer(self.conn, self.ctx, "继续", session_id=first["session_id"])
        self.assertEqual(second["images"][0]["image_occurrence_id"], self.occ)

    def test_all_agent_and_vfs_combinations_recover_second_turn(self):
        for delegating in (False, True):
            for kb in (False, True):
                with self.subTest(delegating=delegating, kb=kb):
                    qa = self._qa(delegating=delegating, kb=kb)
                    query = {"tool": "search_knowledge", "args": {"query": "x"}}
                    script = ([{"tool": "task", "args": {"subagent_type": "retriever", "description": "first"}},
                               query, f"brief [chunk:{self.c1}]"] if delegating else [query])
                    script += [f"answer [chunk:{self.c1}]"]
                    self._model(qa, script)
                    first = qa.answer(self.conn, self.ctx, "q1")
                    model = self._model(qa, [f"continued [chunk:{self.c1}]"])
                    second = qa.answer(self.conn, self.ctx, "q2", session_id=first["session_id"])
                    self.assertEqual(second["citations"][0]["chunk_id"], self.c1)
                    self.assertIn("q1", [m.content for m in model.requests[0]])

    def test_state_backend_files_are_now_durable_per_thread(self):
        qa = self._qa(middleware=False)
        self._model(qa, [{"tool": "write_file", "args": {"file_path": "/note.txt", "content": "PERSISTED_NOTE"}},
                         "first"])
        first = qa.answer(self.conn, self.ctx, "q1")
        model = self._model(qa, [{"tool": "read_file", "args": {"file_path": "/note.txt"}}, "second"])
        qa.answer(self.conn, self.ctx, "q2", session_id=first["session_id"])
        self.assertIn("PERSISTED_NOTE", str(model.requests[-1]))

    def test_stream_session_event_and_done_share_id(self):
        qa = self._qa()
        self._model(qa, [{"tool": "read_chunk", "args": {"chunk_id": self.c1}}, f"answer [chunk:{self.c1}]"])
        events = list(qa.answer_stream(self.ctx, "q1"))
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[0]["session_id"], events[-1]["session_id"])
        self.assertEqual(events[-1]["session_status"], "ready")
        self.assertEqual(self._metadata(qa, events[0]["session_id"])["status"], "ready")

    def test_generator_can_advance_on_different_worker_threads(self):
        qa = self._qa()
        self._model(qa, ["answer with enough chunks to alternate workers"])
        stream = qa.answer_stream(self.ctx, "q")
        sentinel = object()
        events = []
        with ThreadPoolExecutor(max_workers=1) as one, ThreadPoolExecutor(max_workers=1) as two:
            for index in range(100):
                pool = one if index % 2 == 0 else two
                event = pool.submit(next, stream, sentinel).result()
                if event is sentinel:
                    break
                events.append(event)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["session_status"], "ready")

    def test_cancellation_marks_failed_and_releases_lease(self):
        qa = self._qa()
        stream = qa.answer_stream(self.ctx, "q")
        session_id = next(stream)["session_id"]
        stream.close()
        self.assertEqual(self._metadata(qa, session_id)["status"], "failed")
        self.assertEqual(qa._sessions()._active, set())
        with self.assertRaisesRegex(SessionError, "未完成"):
            qa.answer(self.conn, self.ctx, "again", session_id=session_id)

    def test_asgi_send_disconnect_closes_generator_without_waiting_for_gc(self):
        from deephoto.api.routes_qa import Question, ask_stream
        from starlette.requests import ClientDisconnect
        qa = self._qa()
        self._model(qa, ["must not run"])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(qa_service=qa)))
        response = ask_stream(request, Question(question="q"), self.ctx)
        received = []
        async def run():
            async def receive():
                return {"type": "http.disconnect"}
            async def send(message):
                if message["type"] == "http.response.body":
                    received.append(json.loads(message["body"].decode()[5:].strip()))
                    raise OSError("client disconnected")
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        with self.assertRaises(ClientDisconnect):
            asyncio.run(run())
        session_id = received[0]["session_id"]
        self.assertEqual(self._metadata(qa, session_id)["status"], "failed")
        self.assertEqual(qa._sessions()._active, set())
        self.assertEqual(qa._chat_model.calls_made, 0)

    def test_disconnect_after_done_commit_keeps_ready_state(self):
        from deephoto.api.routes_qa import Question, ask_stream
        from starlette.requests import ClientDisconnect
        qa = self._qa()
        self._model(qa, ["first answer"])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(qa_service=qa)))
        response = ask_stream(request, Question(question="q1"), self.ctx)
        received = []
        async def run():
            async def receive():
                return {"type": "http.disconnect"}
            async def send(message):
                if message["type"] == "http.response.body" and message["body"]:
                    event = json.loads(message["body"].decode()[5:].strip())
                    received.append(event)
                    if event["type"] == "done":
                        raise OSError("done delivery failed")
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        with self.assertRaises(ClientDisconnect):
            asyncio.run(run())
        session_id = received[0]["session_id"]
        self.assertEqual(self._metadata(qa, session_id)["status"], "ready")
        self.assertEqual(qa._sessions()._active, set())
        model = self._model(qa, ["second answer"])
        qa.answer(self.conn, self.ctx, "q2", session_id=session_id)
        self.assertIn("first answer", [m.content for m in model.requests[0]])

    def test_partial_stream_does_not_commit_a_ready_session(self):
        qa = self._qa()
        def partial(*args):
            yield {"type": "token", "text": "partial"}
            yield {"type": "warning", "detail": "broken"}
            yield {"type": "done", "answer": "partial", "citations": [], "images": []}
        qa._stream_impl = partial
        events = list(qa.answer_stream(self.ctx, "q"))
        self.assertEqual(events[-1]["session_status"], "failed")
        self.assertEqual(self._metadata(qa, events[0]["session_id"])["status"], "failed")

    def test_model_failure_is_not_replayed_on_follow_up(self):
        class BrokenModel(ScriptedFakeChatModel):
            def _next_message(self):
                raise RuntimeError("PROVIDER_FAILURE")
        qa = self._qa()
        qa._chat_model = BrokenModel(script=[])
        events = list(qa.answer_stream(self.ctx, "q"))
        session_id = events[0]["session_id"]
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self._metadata(qa, session_id)["status"], "failed")
        self._model(qa, ["must not run"])
        with self.assertRaisesRegex(SessionError, "未完成"):
            qa.answer(self.conn, self.ctx, "again", session_id=session_id)
        self.assertEqual(qa._chat_model.calls_made, 0)

    def test_stale_running_marker_after_restart_is_rejected(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        ns, _ = qa._sessions()._keys(self.ctx, session_id)
        metadata = self._metadata(qa, session_id)
        metadata["status"] = "running"
        qa._sessions().store.put(ns, session_id, metadata)
        qa.close()
        restarted = self._qa()
        with self.assertRaisesRegex(SessionError, "未完成"):
            restarted.answer(self.conn, self.ctx, "again", session_id=session_id)

    def test_missing_checkpoint_is_rejected(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        _, thread_id = qa._sessions()._keys(self.ctx, session_id)
        qa._sessions().saver.delete_thread(thread_id)
        with self.assertRaisesRegex(SessionError, "状态缺失"):
            qa.answer(self.conn, self.ctx, "again", session_id=session_id)

    def test_store_commit_failure_does_not_leave_a_ready_session(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        store = qa._sessions().store
        original_put = store.put
        def fail_ready(namespace, key, value, **kwargs):
            if value.get("status") == "ready":
                raise RuntimeError("STORE_COMMIT_FAILED")
            return original_put(namespace, key, value, **kwargs)
        self._model(qa, ["second answer"])
        with patch.object(store, "put", fail_ready):
            with self.assertRaisesRegex(RuntimeError, "STORE_COMMIT_FAILED"):
                qa.answer(self.conn, self.ctx, "q2", session_id=session_id)
        self.assertEqual(self._metadata(qa, session_id)["status"], "failed")
        with self.assertRaisesRegex(SessionError, "未完成"):
            qa.answer(self.conn, self.ctx, "again", session_id=session_id)

    def test_delete_store_failure_can_be_retried_but_session_cannot_resume(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        with patch.object(qa._sessions().store, "delete", side_effect=RuntimeError("DELETE_FAILED")):
            with self.assertRaisesRegex(RuntimeError, "DELETE_FAILED"):
                qa.delete_session(self.ctx, session_id)
        with self.assertRaisesRegex(SessionError, "状态缺失"):
            qa.answer(self.conn, self.ctx, "again", session_id=session_id)
        qa.delete_session(self.ctx, session_id)
        with self.assertRaises(SessionError):
            qa.answer(self.conn, self.ctx, "again", session_id=session_id)

    def test_same_session_concurrent_turn_is_rejected_before_model_call(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        self._model(qa, ["must not run"])
        with qa._sessions().turn(self.ctx, session_id, None, qa._session_profile()):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(qa.answer, None, self.ctx, "overlap", session_id=session_id)
                with self.assertRaisesRegex(RuntimeError, "正在回答"):
                    future.result()
        self.assertEqual(qa._chat_model.calls_made, 0)

    def test_delete_removes_checkpoint_and_store_metadata(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        ns, thread_id = qa._sessions()._keys(self.ctx, session_id)
        qa.delete_session(self.ctx, session_id)
        self.assertIsNone(qa._sessions().store.get(ns, session_id))
        self.assertEqual(list(qa._sessions().saver.list({"configurable": {"thread_id": thread_id}})), [])
        with self.assertRaises(SessionError):
            qa.answer(self.conn, self.ctx, "again", session_id=session_id)

    def test_foreign_user_cannot_delete_session(self):
        qa = self._qa()
        session_id = self._first(qa)["session_id"]
        with self.assertRaises(SessionError):
            qa.delete_session(AuthContext("ta", "other-user"), session_id)
        self.assertEqual(self._metadata(qa, session_id)["status"], "ready")

    def test_close_rejects_active_requests_and_closes_connections(self):
        qa = self._qa()
        stream = qa.answer_stream(self.ctx, "q")
        next(stream)
        with self.assertRaisesRegex(RuntimeError, "仍有问答"):
            qa.close()
        stream.close()
        runtime = qa._sessions()
        qa.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            runtime.store.conn.execute("SELECT 1")
        with self.assertRaises(RuntimeError):
            qa.answer(self.conn, self.ctx, "q")

    def test_settings_default_off_and_bad_boolean_rejected(self):
        with patch.dict("os.environ", {"DEEPHOTO_QA_PERSISTENCE_ENABLED": "false"}):
            settings = load_settings()
            self.assertFalse(settings.qa_persistence_enabled)
            self.assertEqual(settings.qa_checkpoint_db_path.name, "qa-checkpoints.db")
        with patch.dict("os.environ", {"DEEPHOTO_QA_PERSISTENCE_ENABLED": "unknown"}):
            with self.assertRaisesRegex(ValueError, "QA_PERSISTENCE_ENABLED"):
                load_settings()


class PersistenceApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _app(self, script):
        from deephoto.api.app import create_app
        with patch.dict("os.environ", {"DEEPHOTO_DATA_DIR": self.tmp.name,
                                       "DEEPHOTO_QA_PERSISTENCE_ENABLED": "true",
                                       "DEEPHOTO_QA_SUBAGENTS_ENABLED": "false",
                                       "DEEPHOTO_QA_KB_VFS_ENABLED": "false",
                                       "DEEPHOTO_QA_MIDDLEWARE_ENABLED": "false",
                                       "DEEPHOTO_DESCRIPTION_ENABLED": "false"}):
            app = create_app(load_settings())
        app.state.qa_service._chat_model = ScriptedFakeChatModel(script=script)
        return app

    def test_post_session_follow_up_and_delete(self):
        from fastapi.testclient import TestClient
        app = self._app(["answer1", "answer2"])
        with TestClient(app) as client:
            first = client.post("/api/qa", json={"question": "q1"})
            self.assertEqual(first.status_code, 200)
            session_id = first.json()["session_id"]
            second = client.post("/api/qa", json={"question": "q2", "session_id": session_id})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(client.delete("/api/qa/sessions/" + session_id).status_code, 200)
            self.assertEqual(client.post("/api/qa", json={"question": "q3", "session_id": session_id}).status_code, 400)
        with self.assertRaises(sqlite3.ProgrammingError):
            app.state.qa_service._session_runtime.saver.conn.execute("SELECT 1")

    def test_invalid_ids_are_422_unknown_ids_are_400(self):
        from fastapi.testclient import TestClient
        with TestClient(self._app([])) as client:
            self.assertEqual(client.post("/api/qa", json={"question": "q", "session_id": "raw-thread"}).status_code, 422)
            self.assertEqual(client.post("/api/qa", json={"question": "q", "session_id": "sess_" + "0" * 32}).status_code, 400)

    def test_sse_session_event_and_done(self):
        from fastapi.testclient import TestClient
        with TestClient(self._app(["answer"])) as client:
            response = client.post("/api/qa/stream", json={"question": "q"})
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            self.assertEqual(events[0]["type"], "session")
            self.assertEqual(events[-1]["type"], "done")
            self.assertEqual(events[0]["session_id"], events[-1]["session_id"])


if __name__ == "__main__":
    unittest.main()
