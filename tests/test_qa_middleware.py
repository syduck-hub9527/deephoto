"""04 中间件:真实图执行 + 离线假模型;不调用付费接口。"""

from __future__ import annotations

import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel
from test_kb_vfs import _Base

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from deephoto.agent.middleware import HideToolsMiddleware, ToolTrace, ToolTraceMiddleware
from deephoto.agent.qa import QAService
from deephoto.config import load_settings


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.trace = ToolTrace()
        self.mw = ToolTraceMiddleware(self.trace, "main")
        self.req = SimpleNamespace(tool_call={"name": "search_knowledge", "id": "call_1",
                                              "args": {"query": "SECRET_QUERY"}})

    def test_tool_message_is_returned_unchanged_and_payload_omitted(self):
        result = ToolMessage(content="SECRET_TEXT", tool_call_id="call_1")
        self.assertIs(self.mw.wrap_tool_call(self.req, lambda _: result), result)
        encoded = json.dumps(self.trace.snapshot())
        self.assertNotIn("SECRET", encoded)
        self.assertEqual(self.trace.snapshot()["events"][0]["outcome"], "returned")

    def test_command_is_returned_unchanged(self):
        result = Command(update={"messages": [ToolMessage(content="bad", status="error",
                                                          tool_call_id="call_1")]})
        self.assertIs(self.mw.wrap_tool_call(self.req, lambda _: result), result)
        self.assertEqual(self.trace.snapshot()["events"][0]["outcome"], "tool_error")

    def test_error_tool_message_is_distinct_from_exception(self):
        result = ToolMessage(content="bad", status="error", tool_call_id="call_1")
        self.mw.wrap_tool_call(self.req, lambda _: result)
        self.assertEqual(self.trace.snapshot()["events"][0]["outcome"], "tool_error")

    def test_exception_is_rethrown_without_recording_exception_text(self):
        error = RuntimeError("SECRET_ERROR")
        def fail(_):
            raise error
        with self.assertRaises(RuntimeError) as caught:
            self.mw.wrap_tool_call(self.req, fail)
        self.assertIs(caught.exception, error)
        event = self.trace.snapshot()["events"][0]
        self.assertEqual((event["outcome"], event["error_type"]), ("exception", "RuntimeError"))
        self.assertNotIn("SECRET", json.dumps(self.trace.snapshot()))

    def test_async_wrapper_preserves_return_and_exception(self):
        async def run():
            result = ToolMessage(content="SECRET_BASE64", tool_call_id="call_1")
            async def ok(_):
                return result
            self.assertIs(await self.mw.awrap_tool_call(self.req, ok), result)
            async def fail(_):
                raise ValueError("SECRET_ERROR")
            with self.assertRaises(ValueError):
                await self.mw.awrap_tool_call(self.req, fail)
        asyncio.run(run())
        self.assertEqual(self.trace.snapshot()["completed"], 2)
        self.assertNotIn("SECRET", json.dumps(self.trace.snapshot()))

    def test_concurrent_bounded_collection_and_detached_snapshot(self):
        trace = ToolTrace(capacity=10)
        def record(_):
            seq = trace.begin()
            trace.finish(seq, agent="retriever", tool="grep", call_id=str(seq),
                         started=monotonic(), outcome="returned")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(100)))
        snap = trace.snapshot()
        self.assertEqual((snap["started"], snap["completed"], snap["dropped"]), (100, 100, 90))
        self.assertEqual([e["seq"] for e in snap["events"]], list(range(1, 11)))
        snap["events"][0]["agent"] = "mutated"
        self.assertEqual(trace.snapshot()["events"][0]["agent"], "retriever")
        self.assertNotEqual(trace.request_id, ToolTrace().request_id)

    def test_hide_supports_dict_tools_and_async(self):
        class Request:
            tools = [{"name": "read_file"}, {"type": "function", "function": {"name": "grep"}},
                     {"name": "search_knowledge"}]
            def override(self, **kwargs):
                return SimpleNamespace(**kwargs)
        mw = HideToolsMiddleware({"read_file", "grep"})
        self.assertEqual(mw.wrap_model_call(Request(), lambda r: r.tools), [{"name": "search_knowledge"}])
        async def run():
            async def handler(r):
                return r.tools
            self.assertEqual(await mw.awrap_model_call(Request(), handler), [{"name": "search_knowledge"}])
        asyncio.run(run())


class FrameworkFactsTest(unittest.TestCase):
    def test_wrapper_order_and_reverse_unwind(self):
        seen = []
        class First(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                seen.append("first-in")
                result = handler(request)
                seen.append("first-out")
                return result
        class Second(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                seen.append("second-in")
                result = handler(request)
                seen.append("second-out")
                return result
        model = ScriptedFakeChatModel(script=["answer"])
        create_deep_agent(model=model, middleware=[First(), Second()]).invoke(
            {"messages": [{"role": "user", "content": "q"}]})
        self.assertEqual(seen, ["first-in", "second-in", "second-out", "first-out"])

    def test_hide_does_not_block_execution(self):
        calls = []
        def probe(value: str) -> str:
            """探针。"""
            calls.append(value)
            return value
        model = ScriptedFakeChatModel(script=[{"tool": "probe", "args": {"value": "x"}}, "end"])
        agent = create_deep_agent(model=model, tools=[probe], middleware=[HideToolsMiddleware({"probe"})])
        agent.invoke({"messages": [{"role": "user", "content": "q"}]})
        self.assertNotIn("probe", model.bound_tool_names[0])
        self.assertEqual(calls, ["x"])  # 模型仍可构造已隐藏工具调用;不是权限边界

    def test_end_limit_message_is_in_messages_stream(self):
        def probe() -> str:
            """探针。"""
            return "ok"
        model = ScriptedFakeChatModel(script=[{"tool": "probe"}], repeat_last=True)
        agent = create_deep_agent(model=model, tools=[probe],
                                  middleware=[ModelCallLimitMiddleware(run_limit=2, exit_behavior="end")])
        texts = [str(chunk.content) for chunk, _ in agent.stream(
            {"messages": [{"role": "user", "content": "q"}]}, stream_mode="messages")]
        self.assertEqual(model.calls_made, 2)
        self.assertIn("Model call limits exceeded", "".join(texts))

    def test_summarization_offloads_to_composite_default_not_kb(self):
        backend = CompositeBackend(default=StateBackend(), routes={"/kb/": StateBackend()})
        model = ScriptedFakeChatModel(script=["summary of old messages", "final answer"])
        summ = SummarizationMiddleware(model=model, backend=backend,
                                      trigger=("messages", 4), keep=("messages", 2))
        agent = create_deep_agent(model=model, backend=backend,
                                  middleware=[summ, ModelCallLimitMiddleware(run_limit=1)])
        messages = [{"role": "user" if i % 2 == 0 else "assistant",
                     "content": f"old-message-{i}"} for i in range(7)]
        result = agent.invoke({"messages": messages})
        paths = list(result.get("files", {}))
        self.assertTrue(any(p.startswith("/conversation_history/") for p in paths), paths)
        self.assertFalse(any(p.startswith("/kb/") for p in paths))
        self.assertIn("summary of old messages", str(model.requests[-1]))
        self.assertTrue(any(str(m.content) == "old-message-0" for m in result["messages"]))
        self.assertEqual(model.calls_made, 2)  # 摘要的直接模型调用也收费,主级 limit 不统计它
        self.assertEqual(result["messages"][-1].content, "final answer")

    def test_actual_parallel_tool_calls_share_trace(self):
        class MultiToolModel(ScriptedFakeChatModel):
            def _next_message(self):
                if self._cursor == 0:
                    self._cursor += 1
                    return AIMessage(content="", tool_calls=[
                        {"name": "probe", "args": {"value": str(i)}, "id": f"call_{i}"}
                        for i in range(8)])
                return super()._next_message()
        def probe(value: str) -> str:
            """并行工具探针。"""
            return value
        trace = ToolTrace()
        model = MultiToolModel(script=["unused", "answer"])
        agent = create_deep_agent(model=model, tools=[probe], middleware=[ToolTraceMiddleware(trace, "main")])
        agent.invoke({"messages": [{"role": "user", "content": "q"}]})
        snapshot = trace.snapshot()
        self.assertEqual(snapshot["completed"], 8)
        self.assertEqual({e["call_id"] for e in snapshot["events"]}, {f"call_{i}" for i in range(8)})

    def test_actual_async_graph_uses_async_trace_wrapper(self):
        def probe() -> str:
            """异步图内的同步工具。"""
            return "ok"
        trace = ToolTrace()
        model = ScriptedFakeChatModel(script=[{"tool": "probe"}, "answer"])
        agent = create_deep_agent(model=model, tools=[probe], middleware=[ToolTraceMiddleware(trace, "main")])
        asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "q"}]}))
        self.assertEqual(trace.snapshot()["completed"], 1)
        self.assertEqual(trace.snapshot()["events"][0]["tool"], "probe")


class ServiceTest(_Base):
    def _qa(self, *, enabled=True, delegating=False, kb=False, limit=12):
        knowledge = self.vfs._knowledge
        knowledge.search = lambda *args: {"chunks": [{"chunk_id": self.c1, "text": "正文"}], "images": []}
        qa = QAService(SimpleNamespace(db_path=self.db_path, qa_middleware_enabled=enabled,
                                      qa_subagents_enabled=delegating, qa_kb_vfs_enabled=kb,
                                      chat_model=f"fake-04-{int(delegating)}-{int(kb)}",
                                      qa_main_max_model_calls=limit), knowledge)
        return qa

    def test_flag_off_preserves_kwargs_and_response(self):
        qa = self._qa(enabled=False)
        qa._chat_model = ScriptedFakeChatModel(script=["answer"])
        self.assertEqual(qa._agent_kwargs(self.ctx, self.tracker), {})
        result = qa.answer(self.conn, self.ctx, "q")
        self.assertEqual(set(result), {"answer", "citations", "images"})

    def test_all_four_agent_backend_combinations_record_tools_and_preserve_citations(self):
        for delegating in (False, True):
            for kb in (False, True):
                with self.subTest(delegating=delegating, kb=kb):
                    qa = self._qa(delegating=delegating, kb=kb)
                    query = {"tool": "search_knowledge", "args": {"query": "SECRET_QUERY"}}
                    script = ([{"tool": "task", "args": {"description": "SECRET_TASK", "subagent_type": "retriever"}},
                               query, f"要点 [chunk:{self.c1}]"] if delegating else [query])
                    script += [f"answer [chunk:{self.c1}] [chunk:fake]"]
                    qa._chat_model = ScriptedFakeChatModel(script=script, model_name=qa.settings.chat_model,
                                                          ls_provider="openai")
                    result = qa.answer(self.conn, self.ctx, "q")
                    self.assertEqual([c["chunk_id"] for c in result["citations"]], [self.c1])
                    events = result["tool_trace"]["events"]
                    self.assertEqual([(e["agent"], e["tool"]) for e in events],
                                     [("main", "task"), ("retriever", "search_knowledge")] if delegating
                                     else [("main", "search_knowledge")])
                    self.assertNotIn("SECRET", json.dumps(result["tool_trace"]))

    def test_stream_done_has_trace_and_no_subagent_text(self):
        qa = self._qa(delegating=True)
        qa._chat_model = ScriptedFakeChatModel(script=[
            {"tool": "task", "args": {"description": "find", "subagent_type": "retriever"}},
            {"tool": "read_chunk", "args": {"chunk_id": self.c1}}, "SECRET_INTERNAL",
            f"answer [chunk:{self.c1}]"], model_name=qa.settings.chat_model, ls_provider="openai")
        events = list(qa.answer_stream(self.ctx, "q"))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["tool_trace"]["completed"], 2)
        self.assertNotIn("SECRET_INTERNAL", "".join(e["text"] for e in events if e["type"] == "token"))

    def test_request_trace_is_not_reused(self):
        qa = self._qa()
        qa._chat_model = ScriptedFakeChatModel(script=["a", "b"])
        first = qa.answer(self.conn, self.ctx, "q")["tool_trace"]
        second = qa.answer(self.conn, self.ctx, "q")["tool_trace"]
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(second["started"], 0)

    def test_main_loop_limit_ends_with_notice(self):
        qa = self._qa(limit=2)
        qa._chat_model = ScriptedFakeChatModel(script=[{"tool": "search_knowledge", "args": {"query": "x"}}],
                                              repeat_last=True)
        result = qa.answer(self.conn, self.ctx, "q")
        self.assertIn("Model call limits exceeded", result["answer"])
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["tool_trace"]["completed"], 2)
        self.assertEqual(qa._chat_model.calls_made, 2)

    def test_stream_limit_ends_with_notice_and_done(self):
        qa = self._qa(limit=2)
        qa._chat_model = ScriptedFakeChatModel(script=[{"tool": "search_knowledge", "args": {"query": "x"}}],
                                              repeat_last=True)
        events = list(qa.answer_stream(self.ctx, "q"))
        self.assertEqual(events[-1]["type"], "done")
        self.assertIn("Model call limits exceeded", events[-1]["answer"])
        self.assertEqual(qa._chat_model.calls_made, 2)

    def test_vfs_read_is_traced_and_registers_evidence(self):
        qa = self._qa(kb=True)
        qa._chat_model = ScriptedFakeChatModel(script=[
            {"tool": "read_file", "args": {"file_path": f"/kb/{self.doc_a}/content.md"}},
            f"answer [chunk:{self.c1}]"], model_name=qa.settings.chat_model, ls_provider="openai")
        result = qa.answer(self.conn, self.ctx, "q")
        self.assertEqual(result["tool_trace"]["events"][0]["tool"], "read_file")
        self.assertEqual(result["citations"][0]["chunk_id"], self.c1)

    def test_figure_checker_has_own_trace(self):
        qa = self._qa(delegating=True, kb=True)
        qa.knowledge.image_content_blocks = lambda *args: [{"type": "text", "text": "SECRET_PIXELS"}]
        qa._chat_model = ScriptedFakeChatModel(script=[
            {"tool": "task", "args": {"description": "check", "subagent_type": "figure_checker"}},
            {"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},
            "无法确认", "answer"], model_name=qa.settings.chat_model, ls_provider="openai")
        result = qa.answer(self.conn, self.ctx, "q")
        self.assertEqual(result["tool_trace"]["events"][1]["agent"], "figure_checker")
        self.assertNotIn("SECRET_PIXELS", json.dumps(result["tool_trace"]))

    def test_middleware_on_removes_unbounded_general_purpose(self):
        qa = self._qa()
        qa._chat_model = ScriptedFakeChatModel(script=[
            {"tool": "search_knowledge", "args": {"query": "x"}},
            f"answer [chunk:{self.c1}]"],
            model_name=qa.settings.chat_model, ls_provider="openai")
        result = qa.answer(self.conn, self.ctx, "q")
        self.assertEqual([(e["agent"], e["tool"]) for e in result["tool_trace"]["events"]],
                         [("main", "search_knowledge")])
        self.assertNotIn("task", qa._chat_model.bound_tool_names[0])
        self.assertEqual(result["citations"][0]["chunk_id"], self.c1)

    def test_new_settings_validation(self):
        with patch.dict("os.environ", {"DEEPHOTO_QA_MIDDLEWARE_ENABLED": "true",
                                       "DEEPHOTO_QA_MAIN_MAX_MODEL_CALLS": "3"}):
            settings = load_settings()
            self.assertTrue(settings.qa_middleware_enabled)
            self.assertEqual(settings.qa_main_max_model_calls, 3)
        with patch.dict("os.environ", {"DEEPHOTO_QA_MAIN_MAX_MODEL_CALLS": "1"}):
            with self.assertRaisesRegex(ValueError, "QA_MAIN_MAX_MODEL_CALLS"):
                load_settings()


if __name__ == "__main__":
    unittest.main()
