"""截图审查的三项修复:历史载荷、预检失败、默认 GP 限额绕过。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel
from test_kb_vfs import _Base

from deepagents import create_deep_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from deephoto import repo
from deephoto.agent.persistence import SessionError
from deephoto.agent.qa import QAService

PAYLOAD = "IMAGE_SENTINEL" + "A" * 200000


class ServiceRepairsTest(_Base):
    def setUp(self):
        super().setUp()
        self.services = []

    def tearDown(self):
        for qa in self.services:
            qa.close()
        super().tearDown()

    def _qa(self, *, persistence=True, middleware=True, delegating=False, kb=False, skills=False):
        knowledge = self._vfs()._knowledge
        knowledge.image_content_blocks = lambda *args: [
            {"type": "text", "text": f"[image:{self.occ}]"},
            {"type": "image", "base64": PAYLOAD, "mime_type": "image/png"}]
        knowledge.search = lambda *args: {"chunks": [{"chunk_id": self.c1, "text": "正文"}], "images": []}
        qa = QAService(SimpleNamespace(db_path=self.db_path,
            qa_persistence_enabled=persistence, qa_middleware_enabled=middleware,
            qa_subagents_enabled=delegating, qa_kb_vfs_enabled=kb, qa_skills_enabled=skills,
            qa_main_max_model_calls=3, qa_main_recursion_limit=50,
            chat_model=f"fake-repair-{int(middleware)}-{int(delegating)}-{int(kb)}-{int(skills)}"), knowledge)
        self.services.append(qa)
        return qa

    def _model(self, qa, script):
        qa._chat_model = ScriptedFakeChatModel(script=script, model_name=qa.settings.chat_model, ls_provider="openai")
        return qa._chat_model

    def _metadata(self, qa, sid):
        ns, thread = qa._sessions()._keys(self.ctx, sid)
        return qa._sessions().store.get(ns, sid).value

    def test_completed_image_round_not_replayed_next_request(self):
        qa = self._qa()
        model = self._model(qa, [{"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},
                                  f"observed [image:{self.occ}]", f"again [image:{self.occ}]"])
        first = qa.answer(self.conn, self.ctx, "q1")
        second = qa.answer(self.conn, self.ctx, "q2", session_id=first["session_id"])
        self.assertTrue(PAYLOAD in str(model.requests[1]))  # 当轮必须仍能看原图
        self.assertFalse(PAYLOAD in str(model.requests[2]))
        self.assertFalse(any(isinstance(m, ToolMessage) or getattr(m, "tool_calls", []) for m in model.requests[2]))
        self.assertIn("q1", [m.content for m in model.requests[2]])
        self.assertIn(f"observed [image:{self.occ}]", [m.content for m in model.requests[2]])
        self.assertEqual([i["image_occurrence_id"] for i in second["images"]], [self.occ])

    def test_scope_precheck_failure_does_not_poison_ready_session(self):
        qa = self._qa()
        self._model(qa, ["first"])
        sid = qa.answer(self.conn, self.ctx, "q1", document_id=self.doc_a)["session_id"]
        before = dict(self._metadata(qa, sid))
        repo.update_document_status(self.conn, self.doc_a, "queued")
        self._model(qa, ["must not run"])
        try:
            qa.answer(self.conn, self.ctx, "q2", document_id=self.doc_a, session_id=sid)
        except SessionError:
            pass
        self.assertEqual(self._metadata(qa, sid), before)
        self.assertEqual(qa._chat_model.calls_made, 0)
        repo.update_document_status(self.conn, self.doc_a, "ready")
        model = self._model(qa, ["second"])
        self.assertEqual(qa.answer(self.conn, self.ctx, "q3", document_id=self.doc_a, session_id=sid)["answer"], "second")
        self.assertNotIn("q2", [m.content for m in model.requests[0]])

    def test_default_general_purpose_cannot_bypass_main_limit(self):
        qa = self._qa(persistence=False)
        query = {"tool": "search_knowledge", "args": {"query": "x"}}
        model = self._model(qa, [{"tool": "task", "args": {"subagent_type": "general-purpose", "description": "loop"}},
                                 query, query, query, query, "brief", "answer"])
        qa.answer(self.conn, self.ctx, "q")
        self.assertLessEqual(model.calls_made, 3)
        self.assertNotIn("task", model.bound_tool_names[0])

    def test_stream_image_round_is_compacted_and_references_still_validate(self):
        qa = self._qa()
        self._model(qa, [{"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}}, f"observed [image:{self.occ}]"])
        events = list(qa.answer_stream(self.ctx, "q1"))
        sid = events[-1]["session_id"]
        self.assertEqual(events[-1]["session_status"], "ready")
        model = self._model(qa, [f"again [image:{self.occ}]"])
        follow = list(qa.answer_stream(self.ctx, "q2", session_id=sid))
        self.assertFalse(PAYLOAD in str(model.requests[0]))
        self.assertEqual([i["image_occurrence_id"] for i in follow[-1]["images"]], [self.occ])

    def test_upgrade_compacts_old_completed_checkpoint_before_model_call(self):
        qa = self._qa()
        runtime = qa._sessions()
        with runtime.storage() as store:
            sid = "sess_" + "a" * 32
            ns, thread = runtime._keys(self.ctx, sid)
            model = self._model(qa, [{"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}}, "legacy-answer"])
            tools, _ = qa._make_tools(self.ctx)
            create_deep_agent(model=model, tools=tools, checkpointer=runtime.saver).invoke(
                {"messages": [{"role": "user", "content": "legacy-q"}]}, {"configurable": {"thread_id": thread}})
            store.put(ns, sid, {"status": "ready", "document_id": None, "profile": qa._session_profile(),
                                "chunk_ids": [], "image_ids": []})
        model = self._model(qa, ["new-answer"])
        qa.answer(self.conn, self.ctx, "q2", session_id=sid)
        self.assertFalse(PAYLOAD in str(model.requests[0]))
        self.assertIn("legacy-answer", [m.content for m in model.requests[0]])

    def test_subagent_latest_checkpoint_compacts_internal_media(self):
        qa = self._qa(delegating=True)
        self._model(qa, [{"tool": "task", "args": {"subagent_type": "figure_checker", "description": "check"}},
            {"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}}, "checked", f"answer [image:{self.occ}]"])
        result = qa.answer(self.conn, self.ctx, "q")
        runtime = qa._sessions()
        _, thread = runtime._keys(self.ctx, result["session_id"])
        config = {"configurable": {"thread_id": thread}}
        namespaces = {row.config["configurable"].get("checkpoint_ns", "") for row in runtime.saver.list(config)}
        self.assertGreater(len(namespaces), 1)
        tools, tracker = qa._make_tools(self.ctx)
        graph = qa._build_agent(tools, checkpointer=runtime.saver, store=runtime.store,
                                **qa._agent_kwargs(self.ctx, tracker))
        # LangGraph 的消息 delta 可能不在单条原始 checkpoint.channel_values 中;
        # 根状态通过编译图重建,不能把 get_tuple().messages 缺失当成丢历史。
        root_messages = graph.get_state(config).values["messages"]
        self.assertEqual(len(root_messages), 2)
        self.assertFalse(PAYLOAD in str(root_messages))
        for namespace in namespaces:
            if not namespace:
                continue
            latest = runtime.saver.get_tuple({"configurable": {"thread_id": thread, "checkpoint_ns": namespace}})
            messages = latest.checkpoint["channel_values"]["messages"]
            self.assertFalse(any(isinstance(m, ToolMessage) for m in messages))
            self.assertFalse(PAYLOAD in str(messages))
        # 历史 checkpoint 未物理清理,避免把上下文修复描述成数据库回收。
        self.assertTrue(any(PAYLOAD in str(row.checkpoint) for row in runtime.saver.list(config)))

    def test_stream_precheck_keeps_session_and_emits_recoverable_error(self):
        qa = self._qa()
        self._model(qa, ["first"])
        sid = qa.answer(self.conn, self.ctx, "q1", document_id=self.doc_a)["session_id"]
        before = dict(self._metadata(qa, sid))
        repo.update_document_status(self.conn, self.doc_a, "queued")
        self._model(qa, ["must not run"])
        events = list(qa.answer_stream(self.ctx, "q2", document_id=self.doc_a, session_id=sid))
        self.assertEqual(events, [{"type": "error", "detail": "文档不存在、无权限或尚未处理完成",
                                  "code": "document_scope_unavailable", "recoverable": True}])
        self.assertEqual(self._metadata(qa, sid), before)
        self.assertEqual(qa._chat_model.calls_made, 0)

    def test_precheck_failure_does_not_create_a_new_session(self):
        qa = self._qa()
        self._model(qa, ["must not run"])
        try:
            qa.answer(self.conn, self.ctx, "q", document_id=self.doc_q)
        except SessionError:
            pass
        with qa._sessions().storage() as store:
            ns, _ = qa._sessions()._keys(self.ctx, "sess_" + "b" * 32)
            self.assertEqual(store.search(ns), [])
        self.assertEqual(qa._chat_model.calls_made, 0)

    def test_executed_model_failure_remains_failed_and_cannot_resume(self):
        qa = self._qa()
        self._model(qa, ["first"])
        sid = qa.answer(self.conn, self.ctx, "q1")["session_id"]
        with patch.object(qa, "_build_agent", return_value=SimpleNamespace(invoke=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                qa.answer(self.conn, self.ctx, "q2", session_id=sid)
        self.assertEqual(self._metadata(qa, sid)["status"], "failed")
        with self.assertRaises(SessionError):
            qa.answer(self.conn, self.ctx, "q3", session_id=sid)

    def test_general_purpose_disabled_in_single_agent_flag_combinations(self):
        for kb in (False, True):
            for skills in (False, True):
                qa = self._qa(persistence=False, kb=kb, skills=skills)
                model = self._model(qa, ["answer"])
                qa.answer(self.conn, self.ctx, "q")
                self.assertNotIn("task", model.bound_tool_names[0])
                self.assertIn("search_knowledge", model.bound_tool_names[0])

    def test_middleware_off_preserves_default_general_purpose(self):
        qa = self._qa(persistence=False, middleware=False)
        model = self._model(qa, ["answer"])
        qa.answer(self.conn, self.ctx, "q")
        self.assertIn("task", model.bound_tool_names[0])

    def test_persistence_off_preserves_raw_graph_messages(self):
        qa = self._qa(persistence=False)
        self._model(qa, [{"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}}, "answer"])
        tools, tracker = qa._make_tools(self.ctx)
        result = qa._build_agent(tools, **qa._agent_kwargs(self.ctx, tracker)).invoke({"messages": [{"role": "user", "content": "q"}]})
        self.assertTrue(PAYLOAD in str(result["messages"]))


class CompactionFrameworkTest(unittest.TestCase):
    def test_completed_round_preserves_only_user_and_final_text(self):
        from deephoto.agent.history import compact_messages
        user = HumanMessage(content="q", id="q")
        call = AIMessage(content="planning", tool_calls=[{"name": "probe", "args": {}, "id": "call"}], id="call-ai")
        tool_msg = ToolMessage(content=PAYLOAD, tool_call_id="call", id="tool")
        final = AIMessage(content=[{"type": "text", "text": "answer"}], additional_kwargs={"reasoning_content": PAYLOAD}, id="final")
        result = compact_messages([user, call, tool_msg, final])
        self.assertEqual([m.content for m in result], ["q", "answer"])
        self.assertEqual([m.id for m in result], ["q", "final"])
        self.assertFalse(PAYLOAD in str(result))

    def test_pending_tool_round_is_unchanged_and_real_interrupt_resumes(self):
        from deephoto.agent.history import CompletedRoundMiddleware, compact_messages
        messages = [HumanMessage(content="q", id="q"), AIMessage(content="", tool_calls=[{"name": "probe", "args": {}, "id": "call"}], id="call-ai")]
        self.assertEqual(compact_messages(messages), messages)
        partial = [HumanMessage(content="q", id="q"),
                   AIMessage(content="", tool_calls=[{"name": "probe", "args": {}, "id": "first"}]),
                   ToolMessage(content="prior tool result", tool_call_id="first"),
                   AIMessage(content="planning", tool_calls=[{"name": "probe", "args": {}, "id": "second"}])]
        self.assertEqual(compact_messages(partial), partial)
        calls = []
        @tool
        def probe() -> str:
            """执行探针。"""
            calls.append(True)
            return "ok"
        with SqliteSaver.from_conn_string(":memory:") as saver:
            model = ScriptedFakeChatModel(script=[{"tool": "probe"}, "answer"])
            graph = create_deep_agent(model=model, tools=[probe], checkpointer=saver,
                middleware=[CompletedRoundMiddleware()], interrupt_on={"probe": True})
            config = {"configurable": {"thread_id": "pause"}}
            paused = graph.invoke({"messages": [{"role": "user", "content": "q"}]}, config)
            self.assertIn("__interrupt__", paused)
            self.assertEqual(calls, [])
            self.assertTrue(paused["messages"][-1].tool_calls)
            completed = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
            self.assertEqual(calls, [True])
            self.assertEqual([m.content for m in completed["messages"]], ["q", "answer"])

    def test_async_graph_uses_compaction_hooks(self):
        from deephoto.agent.history import CompletedRoundMiddleware
        @tool
        def probe() -> str:
            """返回内部文本。"""
            return PAYLOAD
        model = ScriptedFakeChatModel(script=[{"tool": "probe"}, "answer"])
        graph = create_deep_agent(model=model, tools=[probe], middleware=[CompletedRoundMiddleware()])
        result = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "q"}]}))
        self.assertEqual([m.content for m in result["messages"]], ["q", "answer"])

    def test_summary_cutoff_is_reset_when_messages_are_reindexed(self):
        from deephoto.agent.history import CompletedRoundMiddleware
        # 固定版本摘要状态按原始 messages 的位置切片,删消息必须重置该事件。
        update = CompletedRoundMiddleware().before_agent({"messages": [
            HumanMessage(content="q"), AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "c"}]),
            ToolMessage(content="large", tool_call_id="c"), AIMessage(content="a")],
            "_summarization_event": {"cutoff_index": 3, "summary_message": HumanMessage(content="old-summary")}}, None)
        self.assertIsNone(update["_summarization_event"])


if __name__ == "__main__":
    unittest.main()
