"""deepagents 0.7.19 框架契约测试(md文档 00 §3.1/§3.2 → §4.1 B3)。

把上一轮实测事实固化为离线测试。它们是这个项目的**升级门槛**:
升级 deepagents 之前先跑这一组,任何失败都意味着框架行为变化,必须先评估再升级。

全部使用 tests/support/fakes.py 的 ScriptedFakeChatModel,不依赖真实 API。

覆盖的事实(括号内为文档出处):
- 默认注入工具无 write_todos(§3.1 更正);StateBackend 下 execute 被能力门控隐藏(§3.2)
- 默认 recursion_limit=9999,必须显式收窄(§3.2)
- FilesystemMiddleware(tools=[...]) 收窄工具且必须保留 read_file(§3.2)
- 关掉 general-purpose 子智能体后 task 消失(§3.2)
- 自定义后端挂 /kb/ 路由,read_file 读 .png 原生返回图片块(§3.2)
- 流式加 subgraphs=True 才收到子智能体事件;主智能体 token 用 ns == () 过滤(§3.2;
  文档原文写 subagents=True,langgraph 1.2.14 的实际参数名是 subgraphs)
- 主智能体中间件看不到子智能体内部工具调用(§3.2)
- SqliteSaver + thread_id 跨轮保留历史,delete_thread 可用(§3.2)
- StoreBackend + SqliteStore 的记忆注入系统提示(§3.2)
- Skills 系统提示只放名称和描述(§3.2)
- interrupt_on 需要 checkpointer,用 Command(resume=...) 恢复(§3.2)
- 06 HITL 事实 F1–F12 与待定项 U1/U2/U3(md文档/files/06-human-in-the-loop.md §2)
"""

from __future__ import annotations

import base64
import unittest
import unittest.mock

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
)
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import BackendProtocol, ReadResult
from deepagents.backends.state import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import AgentMiddleware, InterruptOnConfig
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore
from langgraph.types import Command


def _make_probe_tool():
    """记录调用次数的探针工具(每个测试独立实例,避免串状态)。"""
    calls: list[str] = []

    @tool
    def probe_tool(text: str) -> str:
        """探针工具:记录调用并回显输入。"""
        calls.append(text)
        return f"probe:{text}"

    return probe_tool, calls


def _last_ai_text(result: dict) -> str:
    for msg in reversed(result["messages"]):
        if getattr(msg, "type", None) == "ai" and msg.content:
            return str(msg.content)
    return ""


class DefaultToolsContract(unittest.TestCase):
    """§3.1 更正 + §3.2 第 1 行:默认工具集、execute 隐藏、无 write_todos。"""

    def test_default_visible_tool_set(self):
        """默认模型可见工具恰为 ls/read_file/write_file/edit_file/delete/glob/grep/task。

        execute 虽在默认注入清单(§3.1),但默认 StateBackend 不支持命令执行,
        每请求被能力门控过滤(§3.2"自动隐藏");delete 因 StateBackend 实现了
        delete 而保留。无 write_todos(§3.1 更正:0.7.19 不默认注入规划工具)。
        """
        model = ScriptedFakeChatModel(script=[{"text": "好"}])
        agent = create_deep_agent(model=model, system_prompt="契约测试")
        agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
        visible = set(model.bound_tool_names[-1])
        self.assertEqual(
            visible,
            {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "task"},
        )

    def test_no_write_todos_by_default(self):
        """§3.1 更正单列一条:升级时若框架重新默认注入 write_todos,这条会抓到。"""
        model = ScriptedFakeChatModel(script=[{"text": "好"}])
        agent = create_deep_agent(model=model, system_prompt="契约测试")
        agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
        for bound in model.bound_tool_names:
            self.assertNotIn("write_todos", bound)

    def test_recursion_limit_default_is_9999(self):
        """默认 recursion_limit=9999(§3.2)——项目必须显式收窄,这里钉住默认值。"""
        model = ScriptedFakeChatModel(script=[{"text": "好"}])
        agent = create_deep_agent(model=model, system_prompt="契约测试")
        self.assertEqual(agent.config["recursion_limit"], 9_999)


class FilesystemNarrowingContract(unittest.TestCase):
    """§3.2 第 3 行:FilesystemMiddleware(tools=[...]) 收窄内置工具。"""

    def test_allowlist_replaces_default_filesystem_tools(self):
        """传入同名 FilesystemMiddleware 会替换默认实现,文件工具只剩白名单。"""
        model = ScriptedFakeChatModel(script=[{"text": "好"}])
        agent = create_deep_agent(
            model=model,
            system_prompt="契约测试",
            middleware=[FilesystemMiddleware(tools=["ls", "read_file", "grep", "glob"])],
        )
        agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
        visible = set(model.bound_tool_names[-1])
        # 文件工具只剩白名单;task 来自 SubAgentMiddleware,不受影响
        self.assertEqual(visible, {"ls", "read_file", "grep", "glob", "task"})

    def test_allowlist_must_include_read_file(self):
        """tools 列表不含 read_file 时构造即报 ValueError。"""
        with self.assertRaises(ValueError):
            FilesystemMiddleware(tools=["ls", "grep"])


class GeneralPurposeSubagentContract(unittest.TestCase):
    """§3.2 第 3 行:关掉 general-purpose 子智能体后 task 工具消失。"""

    def test_disabling_general_purpose_removes_task(self):
        profile = HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))
        # 假模型的 provider 解析为 "scriptedfakechatmodel";用 patch.dict 临时注册,测完自动还原
        with unittest.mock.patch.dict(
            "deepagents.profiles.harness.harness_profiles._HARNESS_PROFILES",
            {"scriptedfakechatmodel": profile},
        ):
            model = ScriptedFakeChatModel(script=[{"text": "好"}])
            agent = create_deep_agent(model=model, system_prompt="契约测试")
            agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
            self.assertNotIn("task", set(model.bound_tool_names[-1]))


class KnowledgeVfsContract(unittest.TestCase):
    """§3.2 第 4 行:自定义后端挂 /kb/ 路由,read_file 读 .png 原生返回图片块。"""

    def test_composite_backend_read_png_returns_image_block(self):
        png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n-fake-png-bytes").decode()

        class KbBackend(BackendProtocol):
            """只读内存知识库后端:CompositeBackend 会剥掉 /kb/ 前缀再调用。"""

            def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
                if file_path == "/pic.png":
                    return ReadResult(file_data={"content": png_b64, "encoding": "base64"})
                return ReadResult(error=f"File '{file_path}' not found")

        backend = CompositeBackend(default=StateBackend(), routes={"/kb/": KbBackend()})
        model = ScriptedFakeChatModel(script=[
            {"tool": "read_file", "args": {"file_path": "/kb/pic.png"}},
            {"text": "已看到图片"},
        ])
        agent = create_deep_agent(model=model, system_prompt="契约测试", backend=backend)
        result = agent.invoke({"messages": [{"role": "user", "content": "看 /kb/pic.png"}]})

        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_msgs), 1)
        blocks = tool_msgs[0].content_blocks
        image_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "image"]
        self.assertEqual(len(image_blocks), 1)
        self.assertEqual(image_blocks[0]["mime_type"], "image/png")
        self.assertEqual(image_blocks[0]["base64"], png_b64)


class StreamingContract(unittest.TestCase):
    """§3.2 第 5 行:子智能体事件与命名空间过滤。"""

    def _build(self):
        """主智能体脚本:先调 task 委派子智能体,再给最终回答。"""
        @tool
        def sub_search(keyword: str) -> str:
            """子智能体专用检索(离线桩)。"""
            return f"sub:{keyword}"

        model = ScriptedFakeChatModel(script=[
            {"tool": "task", "args": {"description": "查一下", "subagent_type": "researcher"}},
            {"tool": "sub_search", "args": {"keyword": "k"}},   # 子智能体内部调用
            {"text": "子智能体结论"},                            # 子智能体收尾
            {"text": "主智能体最终回答"},                        # 主智能体收尾
        ])
        agent = create_deep_agent(
            model=model,
            system_prompt="契约测试",
            subagents=[{
                "name": "researcher",
                "description": "检索研究员",
                "system_prompt": "你是子智能体",
                "tools": [sub_search],
            }],
        )
        return model, agent

    def test_stream_with_subgraphs_separates_namespaces(self):
        """subgraphs=True 时:主智能体 token 的 ns == ();子智能体事件 ns 非空。"""
        _model, agent = self._build()
        main_texts: list[str] = []
        sub_texts: list[str] = []
        for ns, (chunk, _meta) in agent.stream(
            {"messages": [{"role": "user", "content": "hi"}]},
            stream_mode="messages",
            subgraphs=True,
        ):
            if not isinstance(chunk, AIMessageChunk) or not chunk.content:
                continue
            (main_texts if ns == () else sub_texts).append(str(chunk.content))
        self.assertEqual("".join(main_texts), "主智能体最终回答")
        self.assertIn("子智能体结论", "".join(sub_texts))

    def test_stream_without_subgraphs_hides_subagent_chunks(self):
        """现状 qa.py 的用法:不加 subgraphs,只能拿到主智能体层面的流。"""
        _model, agent = self._build()
        texts: list[str] = []
        for chunk, _meta in agent.stream(
            {"messages": [{"role": "user", "content": "hi"}]},
            stream_mode="messages",
        ):
            if isinstance(chunk, AIMessageChunk) and chunk.content:
                texts.append(str(chunk.content))
        joined = "".join(texts)
        self.assertIn("主智能体最终回答", joined)
        self.assertNotIn("子智能体结论", joined)


class MiddlewareVisibilityContract(unittest.TestCase):
    """§3.2 第 6 行:主智能体中间件看不到子智能体内部的工具调用。"""

    def test_main_middleware_only_sees_main_level_tools(self):
        seen: list[str] = []

        class ToolSpy(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                seen.append(request.tool_call["name"])
                return handler(request)

        @tool
        def sub_only_tool(keyword: str) -> str:
            """只有子智能体持有的工具。"""
            return f"sub:{keyword}"

        model = ScriptedFakeChatModel(script=[
            {"tool": "task", "args": {"description": "查一下", "subagent_type": "researcher"}},
            {"tool": "sub_only_tool", "args": {"keyword": "k"}},
            {"text": "子智能体结论"},
            {"text": "主智能体最终回答"},
        ])
        agent = create_deep_agent(
            model=model,
            system_prompt="契约测试",
            middleware=[ToolSpy()],
            subagents=[{
                "name": "researcher",
                "description": "检索研究员",
                "system_prompt": "你是子智能体",
                "tools": [sub_only_tool],
            }],
        )
        agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
        # 主智能体中间件只见 task;sub_only_tool 在子智能体内部执行,不可见
        self.assertEqual(seen, ["task"])


class PersistenceContract(unittest.TestCase):
    """§3.2 第 7 行:SqliteSaver 跨轮历史与 delete_thread。"""

    def _invoke(self, agent, thread_id: str, text: str):
        return agent.invoke(
            {"messages": [{"role": "user", "content": text}]},
            config={"configurable": {"thread_id": thread_id}},
        )

    def test_checkpointer_keeps_history_across_turns(self):
        """同一 thread_id 的第二次请求能见到第一轮的消息。"""
        model = ScriptedFakeChatModel(script=[{"text": "第一轮回答"}, {"text": "第二轮回答"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = create_deep_agent(model=model, system_prompt="契约测试", checkpointer=saver)
            self._invoke(agent, "t1", "第一轮问题")
            self._invoke(agent, "t1", "第二轮问题")
        second_request = model.requests[-1]
        contents = [str(m.content) for m in second_request]
        self.assertIn("第一轮问题", contents)
        self.assertIn("第一轮回答", contents)

    def test_delete_thread_clears_history(self):
        """delete_thread 后同一 thread_id 重新开始,不再带历史。"""
        model = ScriptedFakeChatModel(script=[{"text": "第一轮回答"}, {"text": "第二轮回答"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = create_deep_agent(model=model, system_prompt="契约测试", checkpointer=saver)
            self._invoke(agent, "t1", "第一轮问题")
            saver.delete_thread("t1")
            self._invoke(agent, "t1", "第二轮问题")
        second_request = model.requests[-1]
        contents = [str(m.content) for m in second_request]
        self.assertNotIn("第一轮问题", contents)
        self.assertNotIn("第一轮回答", contents)

    def test_store_backend_memory_injected_into_system_prompt(self):
        """StoreBackend + SqliteStore:memory 文件内容注入系统提示。"""
        from deepagents.backends.store import StoreBackend

        with SqliteStore.from_conn_string(":memory:") as store:
            backend = StoreBackend(namespace=lambda _rt: ("contract",), store=store)
            backend.write("/memory/AGENTS.md", "记住暗号 ZXQ-772:回答前先复述暗号")
            model = ScriptedFakeChatModel(script=[{"text": "好"}])
            agent = create_deep_agent(
                model=model, system_prompt="契约测试",
                backend=backend, store=store, memory=["/memory/AGENTS.md"],
            )
            agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
        system_text = str(model.requests[0][0].content)
        self.assertIn("ZXQ-772", system_text)


class SkillsContract(unittest.TestCase):
    """§3.2 第 8 行:skills 渐进披露——系统提示只放名称和描述。"""

    def test_system_prompt_has_skill_index_but_not_body(self):
        skill_md = (
            "---\n"
            "name: citation-rules\n"
            "description: 引用格式规范演示\n"
            "---\n"
            "\n"
            "技能正文独特句子:凡引用必须重复三遍咒语 ABC。\n"
        )
        model = ScriptedFakeChatModel(script=[{"text": "好"}])
        agent = create_deep_agent(model=model, system_prompt="契约测试", skills=["/skills/"])
        agent.invoke({
            "messages": [{"role": "user", "content": "hi"}],
            # files 通道存 FileData 结构,不是纯字符串
            "files": {"/skills/citation-rules/SKILL.md": {"content": skill_md, "encoding": "utf-8"}},
        })
        system_text = str(model.requests[0][0].content)
        self.assertIn("citation-rules", system_text)
        self.assertIn("引用格式规范演示", system_text)
        self.assertNotIn("三遍咒语", system_text)


class HumanInTheLoopContract(unittest.TestCase):
    """§3.2 第 9 行:interrupt_on 需要 checkpointer;Command(resume=...) 恢复。"""

    def test_interrupt_pauses_tool_and_resume_approve_runs_it(self):
        probe_tool, calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}},
            {"text": "工具已执行完毕"},
        ])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = create_deep_agent(
                model=model, system_prompt="契约测试",
                tools=[probe_tool],
                interrupt_on={"probe_tool": True},
                checkpointer=saver,
            )
            config = {"configurable": {"thread_id": "hitl-1"}}
            paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}]}, config=config)
            self.assertIn("__interrupt__", paused)   # 在工具执行前暂停
            self.assertEqual(calls, [])              # 工具尚未执行
            resumed = agent.invoke(
                Command(resume={"decisions": [{"type": "approve"}]}), config=config)
            self.assertEqual(calls, ["x"])           # 批准后工具真正执行
            self.assertEqual(_last_ai_text(resumed), "工具已执行完毕")

    def test_resume_requires_checkpointer(self):
        """无 checkpointer:中断仍会暂停并返回 __interrupt__,但 Command(resume=...) 报错。

        §3.2"interrupt_on 需要 checkpointer"的精确含义:暂停本身不需要,
        恢复需要——没有持久化状态,RuntimeError: Cannot use Command(resume=...) without checkpointer。
        """
        probe_tool, calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}},
            {"text": "不会走到"},
        ])
        agent = create_deep_agent(
            model=model, system_prompt="契约测试",
            tools=[probe_tool], interrupt_on={"probe_tool": True},
        )
        paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("__interrupt__", paused)
        self.assertEqual(calls, [])
        with self.assertRaises(RuntimeError):
            agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}))


class HitlDeepContract(unittest.TestCase):
    """06 文档 §2 的框架事实 F1–F12 与待验证项 U1/U2/U3(离线探针固化,升级门槛)。

    F1(批准前不执行)与基本 approve 流已由 HumanInTheLoopContract 覆盖,这里不再重复。
    """

    @staticmethod
    def _single(model, saver, tools, interrupt_on):
        return create_deep_agent(model=model, system_prompt="契约测试", tools=tools,
                                 interrupt_on=interrupt_on, checkpointer=saver)

    @staticmethod
    def _interrupts(result):
        return list(result["__interrupt__"])

    def test_f2_subagent_interrupt_bubbles_with_action_requests(self):
        """F2:子智能体中断冒泡到主图;__interrupt__ 的 value 是 dict,含 action_requests/review_configs。"""
        probe_tool, _calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "task", "args": {"description": "d", "subagent_type": "worker"}},
            {"tool": "probe_tool", "args": {"text": "p"}},
            {"text": "子智能体结论"}, {"text": "主最终"},
        ])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = create_deep_agent(
                model=model, system_prompt="契约测试", tools=[],
                subagents=[{"name": "worker", "description": "d", "system_prompt": "s",
                            "tools": [probe_tool]}],
                interrupt_on={"probe_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"])},
                checkpointer=saver)
            paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}]},
                                  config={"configurable": {"thread_id": "f2"}})
        interrupts = self._interrupts(paused)
        self.assertEqual(len(interrupts), 1)
        value = interrupts[0].value
        self.assertIsInstance(value, dict)
        self.assertEqual([a["name"] for a in value["action_requests"]], ["probe_tool"])
        self.assertEqual(value["action_requests"][0]["args"], {"text": "p"})
        self.assertIn("description", value["action_requests"][0])
        self.assertEqual(value["review_configs"][0]["allowed_decisions"], ["approve", "reject"])

    def test_f3_top_level_interrupt_on_inherited_only_when_key_absent(self):
        """F3:规格不带 interrupt_on 键才继承顶层;写 None 或 {} 都会关掉该子智能体的闸门。"""
        for spec_extra, expect_pause in (({}, True), ({"interrupt_on": None}, False), ({"interrupt_on": {}}, False)):
            probe_tool, _calls = _make_probe_tool()
            model = ScriptedFakeChatModel(script=[
                {"tool": "task", "args": {"description": "d", "subagent_type": "worker"}},
                {"tool": "probe_tool", "args": {"text": "p"}},
                {"text": "子智能体结论"}, {"text": "主最终"},
            ])
            spec = {"name": "worker", "description": "d", "system_prompt": "s",
                    "tools": [probe_tool], **spec_extra}
            with SqliteSaver.from_conn_string(":memory:") as saver:
                agent = create_deep_agent(
                    model=model, system_prompt="契约测试", tools=[], subagents=[spec],
                    interrupt_on={"probe_tool": True}, checkpointer=saver)
                result = agent.invoke({"messages": [{"role": "user", "content": "hi"}]},
                                      config={"configurable": {"thread_id": "f3"}})
            self.assertEqual("__interrupt__" in result, expect_pause, spec_extra)

    def test_f4_approve_executes_once_and_resume_does_not_recall_model(self):
        """F4:批准后工具恰好执行一次;恢复本身不重新调用模型(下一请求里已有工具结果)。"""
        probe_tool, calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}}, {"text": "完毕"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool], {"probe_tool": True})
            config = {"configurable": {"thread_id": "f4"}}
            paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}], }, config=config)
            self.assertEqual(model.calls_made, 1)
            resumed = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)
        self.assertEqual(calls, ["x"])
        self.assertEqual(model.calls_made, 2)      # 只有一次收尾调用;恢复步骤本身不调模型
        self.assertIn("probe:x", [str(m.content) for m in model.requests[-1]
                                  if isinstance(m, ToolMessage)])
        self.assertEqual(_last_ai_text(resumed), "完毕")

    def test_f5_reject_skips_tool_and_model_receives_rejection_text(self):
        """F5:拒绝后工具不执行;模型收到拒绝文本并继续收尾。"""
        probe_tool, calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}}, {"text": "无法确认"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool], {"probe_tool": True})
            config = {"configurable": {"thread_id": "f5"}}
            agent.invoke({"messages": [{"role": "user", "content": "hi"}]}, config=config)
            resumed = agent.invoke(Command(resume={"decisions": [{"type": "reject"}]}), config=config)
        self.assertEqual(calls, [])
        tool_texts = [str(m.content) for m in model.requests[-1] if isinstance(m, ToolMessage)]
        self.assertTrue(any("rejected" in t for t in tool_texts))
        self.assertEqual(_last_ai_text(resumed), "无法确认")

    def test_f6_same_message_calls_batch_into_one_interrupt(self):
        """F6:同一条 AI 消息的多个受闸门调用合并为一次中断;决定数不符报 ValueError。"""
        probe_tool, calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tools": [{"name": "probe_tool", "args": {"text": "a"}},
                       {"name": "probe_tool", "args": {"text": "b"}}]},
            {"text": "完毕"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool], {"probe_tool": True})
            config = {"configurable": {"thread_id": "f6"}}
            paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}]}, config=config)
            interrupts = self._interrupts(paused)
            self.assertEqual(len(interrupts), 1)
            self.assertEqual(len(interrupts[0].value["action_requests"]), 2)
            with self.assertRaises(ValueError):
                agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)

    def test_f7_wrong_decision_count_poisons_pending_thread(self):
        """F7:决定数错误的恢复失败后,同一线程再提交正确数量仍报错(线程被毁)。"""
        probe_tool, _calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tools": [{"name": "probe_tool", "args": {"text": "a"}},
                       {"name": "probe_tool", "args": {"text": "b"}}]},
            {"text": "完毕"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool], {"probe_tool": True})
            config = {"configurable": {"thread_id": "f7"}}
            agent.invoke({"messages": [{"role": "user", "content": "hi"}]}, config=config)
            with self.assertRaises(ValueError):
                agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)
            with self.assertRaises(ValueError):
                agent.invoke(Command(resume={"decisions": [{"type": "approve"},
                                                           {"type": "approve"}]}), config=config)

    def test_f8_stream_mode_messages_goes_silent_on_pause(self):
        """F8:stream_mode="messages" 下挂起没有中断信号:无正文块流出,流静默结束。"""
        probe_tool, _calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}}, {"text": "不应流出"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool], {"probe_tool": True})
            config = {"configurable": {"thread_id": "f8"}}
            chunks = [c for c, _meta in agent.stream(
                {"messages": [{"role": "user", "content": "hi"}]},
                stream_mode="messages", config=config)]
            text_chunks = [c for c in chunks
                           if isinstance(c, AIMessageChunk) and c.content]
            self.assertEqual(text_chunks, [])        # 挂起前没有任何正文 token
            state = agent.get_state(config)          # 只能靠 get_state 发现挂起(F9)
            self.assertTrue(any(t.interrupts for t in state.tasks))

    def test_f9_pending_pause_detectable_via_get_state(self):
        """F9:get_state 的 next 指向 HumanInTheLoopMiddleware.after_model,tasks 带 interrupts。"""
        probe_tool, _calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}}, {"text": "完毕"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool], {"probe_tool": True})
            config = {"configurable": {"thread_id": "f9"}}
            agent.invoke({"messages": [{"role": "user", "content": "hi"}]}, config=config)
            state = agent.get_state(config)
        self.assertEqual(state.next, ("HumanInTheLoopMiddleware.after_model",))
        self.assertTrue(any(t.interrupts for t in state.tasks))

    def test_f10_paused_result_last_ai_text_is_empty(self):
        """F10:挂起时 invoke 结果的最后一条 AI 消息正文为空串(调用轮消息)。"""
        probe_tool, _calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}}, {"text": "完毕"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool], {"probe_tool": True})
            paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}]},
                                  config={"configurable": {"thread_id": "f10"}})
        last_ai = next(m for m in reversed(paused["messages"]) if isinstance(m, AIMessage))
        self.assertEqual(str(last_ai.content), "")
        self.assertTrue(last_ai.tool_calls)

    def test_f11_when_predicate_sees_only_subagent_local_messages(self):
        """F11:子智能体内的 when 谓词只能读到子智能体自己的 messages(看不到父级问题)。"""
        probe_tool, _calls = _make_probe_tool()
        observed: list[list[str]] = []

        def when(req):
            observed.append([str(getattr(m, "content", "")) for m in req.state["messages"]])
            return False                                # 不暂停,只观察

        model = ScriptedFakeChatModel(script=[
            {"tool": "task", "args": {"description": "子任务描述", "subagent_type": "worker"}},
            {"tool": "probe_tool", "args": {"text": "p"}},
            {"text": "子智能体结论"}, {"text": "主最终"},
        ])
        agent = create_deep_agent(
            model=model, system_prompt="契约测试", tools=[],
            subagents=[{"name": "worker", "description": "d", "system_prompt": "s",
                        "tools": [probe_tool]}],
            interrupt_on={"probe_tool": InterruptOnConfig(
                allowed_decisions=["approve", "reject"], when=when)})
        agent.invoke({"messages": [{"role": "user", "content": "PARENT_MARKER 问题"}]})
        self.assertEqual(len(observed), 1)
        flat = "\n".join(observed[0])
        self.assertNotIn("PARENT_MARKER", flat)         # 父级消息不可见:基于状态的全局计数恒为 0
        self.assertIn("子任务描述", flat)               # 只有任务说明

    def test_f12_true_expands_to_all_four_decisions(self):
        """F12:interrupt_on 值为 True 时展开为 approve/edit/reject/respond 全部开放。"""
        probe_tool, _calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}}, {"text": "完毕"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool], {"probe_tool": True})
            paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}]},
                                  config={"configurable": {"thread_id": "f12"}})
        configs = self._interrupts(paused)[0].value["review_configs"]
        self.assertEqual(configs[0]["allowed_decisions"],
                         ["approve", "edit", "reject", "respond"])

    def test_u1_parallel_subagent_interrupts_resume_by_id(self):
        """U1:两个并行子智能体各自中断 -> __interrupt__ 含两项;按 interrupt id 映射恢复。"""
        probe_tool, calls = _make_probe_tool()
        model = ScriptedFakeChatModel(script=[
            {"tools": [{"name": "task", "args": {"description": "A", "subagent_type": "wa"}},
                       {"name": "task", "args": {"description": "B", "subagent_type": "wb"}}]},
            {"tool": "probe_tool", "args": {"text": "p"}},
            {"tool": "probe_tool", "args": {"text": "p"}},
            {"text": "子结论"}, {"text": "子结论"}, {"text": "主最终"},
        ])
        specs = [{"name": n, "description": "d", "system_prompt": "s", "tools": [probe_tool]}
                 for n in ("wa", "wb")]
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = create_deep_agent(
                model=model, system_prompt="契约测试", tools=[], subagents=specs,
                interrupt_on={"probe_tool": True}, checkpointer=saver)
            config = {"configurable": {"thread_id": "u1"}}
            paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}]}, config=config)
            interrupts = self._interrupts(paused)
            self.assertEqual(len(interrupts), 2)
            self.assertEqual(len({i.id for i in interrupts}), 2)
            resume = {i.id: {"decisions": [{"type": "approve"}]} for i in interrupts}
            done = agent.invoke(Command(resume=resume), config=config)
        self.assertEqual(sorted(calls), ["p", "p"])
        self.assertEqual(_last_ai_text(done), "主最终")

    def test_u2_resume_reruns_after_model_and_when(self):
        """U2:恢复时 after_model 整体重跑,when 谓词对同一 tool_call_id 再次求值。

        这是 ImageApprovalGate 必须按 tool_call_id 记忆决定的原因:重放时计数不能翻倍。
        """
        probe_tool, _calls = _make_probe_tool()
        when_calls: list[str] = []

        def when(req):
            when_calls.append(req.tool_call["id"])
            return True

        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "x"}}, {"text": "完毕"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool],
                                 {"probe_tool": InterruptOnConfig(
                                     allowed_decisions=["approve", "reject"], when=when)})
            config = {"configurable": {"thread_id": "u2"}}
            agent.invoke({"messages": [{"role": "user", "content": "hi"}]}, config=config)
            self.assertEqual(len(when_calls), 1)
            agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)
        self.assertEqual(len(when_calls), 2)
        self.assertEqual(when_calls[0], when_calls[1])

    def test_u3_description_callback_may_open_own_sqlite_connection(self):
        """U3:description 回调在图节点上下文里执行,自取连接的 sqlite 访问安全。"""
        probe_tool, _calls = _make_probe_tool()
        seen_threads: list[str] = []

        def describe(tool_call, state, runtime):
            import sqlite3
            import threading
            seen_threads.append(threading.current_thread().name)
            conn = sqlite3.connect(":memory:")          # 回调内自取连接,不跨线程复用
            try:
                conn.execute("create table t (v text)")
                conn.execute("insert into t values ('图3 第5页')")
                row = conn.execute("select v from t").fetchone()
            finally:
                conn.close()
            return f"查看原图 {tool_call['args']['text']}({row[0]})"

        model = ScriptedFakeChatModel(script=[
            {"tool": "probe_tool", "args": {"text": "occ_1"}}, {"text": "完毕"}])
        with SqliteSaver.from_conn_string(":memory:") as saver:
            agent = self._single(model, saver, [probe_tool],
                                 {"probe_tool": InterruptOnConfig(
                                     allowed_decisions=["approve", "reject"],
                                     description=describe)})
            paused = agent.invoke({"messages": [{"role": "user", "content": "hi"}]},
                                  config={"configurable": {"thread_id": "u3"}})
        request = self._interrupts(paused)[0].value["action_requests"][0]
        self.assertEqual(request["description"], "查看原图 occ_1(图3 第5页)")
        self.assertTrue(seen_threads)


if __name__ == "__main__":
    unittest.main()
