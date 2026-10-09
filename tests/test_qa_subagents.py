"""委派模式(01-subagents)离线测试:假模型 + 假工具,不依赖真实付费 API。

对应开发文档 01-subagents §5.4 的 11 个用例。与补丁参考实现的差异仅在对
fakes 的适配:仓库的假模型是基线阶段(B3)的 ScriptedFakeChatModel
(bound_tool_names / requests / calls_made),语义与补丁的 ScriptedModel 相同。

注意:register_harness 是进程内全局注册,本文件的假模型统一用
model_name="fake-k3" + ls_provider="openai",profile key 为 "openai:fake-k3",
不会碰到契约测试(key "scriptedfakechatmodel")与真实模型("openai:k3")。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel

from deephoto import repo
from deephoto.agent import subagents
from deephoto.agent.knowledge import KnowledgeService
from deephoto.agent.qa import SYSTEM_PROMPT, QAService
from deephoto.db import connect, init_db
from deephoto.security import AuthContext


def _fake_model(script: list, **kwargs) -> ScriptedFakeChatModel:
    """profile key 对齐 openai:fake-k3,使 register_harness 的排除生效。"""
    return ScriptedFakeChatModel(script=script, model_name="fake-k3", ls_provider="openai", **kwargs)


def _fake_tools(tracker: dict, chunk_id: str) -> list:
    def search_knowledge(query: str, document_id: str = "") -> str:
        """检索知识库,返回正文块与候选图片。"""
        tracker["chunks"].add(chunk_id)
        return json.dumps({"chunks": [{"chunk_id": chunk_id, "text": "正文"}], "images": []})

    def read_chunk(chunk_id: str, neighbors: int = 0) -> str:
        """读取正文块全文。"""
        return json.dumps({"chunks": []})

    def inspect_image(image_occurrence_id: str) -> list:
        """查看原图内容块。"""
        tracker["images"].add(image_occurrence_id)
        return [{"type": "text", "text": "图"}]

    return [search_knowledge, read_chunk, inspect_image]


class DelegationTest(unittest.TestCase):
    """主智能体委派 → 子智能体执行 → 主智能体撰写 的核心行为(01 §3)。"""

    def setUp(self):
        subagents.register_harness("fake-k3")
        self.tracker = {"chunks": set(), "images": set()}
        self.tools = _fake_tools(self.tracker, "c1")

    def _agent(self, main_script, sub_script, *, sub_repeat_last=False, **limits):
        main = _fake_model(main_script)
        sub = _fake_model(sub_script, repeat_last=sub_repeat_last)
        # 子智能体不显式给 model 时继承主模型;这里给子智能体独立的假模型,
        # 才能分别断言两边的可见工具与输入(规格本身由 spec["model"] 覆盖)
        specs = subagents.build_subagent_specs(self.tools, **limits)
        for spec in specs:
            spec["model"] = sub
        from deepagents import create_deep_agent
        agent = create_deep_agent(
            model=main, tools=[], system_prompt=subagents.DELEGATING_SYSTEM_PROMPT,
            subagents=specs)
        return agent, main, sub

    def _ask(self, agent):
        return agent.invoke({"messages": [{"role": "user", "content": "问题"}]},
                            config={"recursion_limit": 40})

    def test_tool_visibility_and_no_general_purpose(self):
        """主智能体只有 task;retriever 只有两个检索工具;task 列表无 general-purpose。"""
        agent, main, sub = self._agent(
            [{"tool": "task", "args": {"description": "找X", "subagent_type": "retriever"}},
             "答案 [chunk:c1]"],
            [{"tool": "search_knowledge", "args": {"query": "X"}}, "【要点】- X [chunk:c1]"])
        self._ask(agent)
        self.assertEqual(main.bound_tool_names[0], ["task"])
        self.assertEqual(sub.bound_tool_names[0], ["search_knowledge", "read_chunk"])
        # task 工具描述里的可选子智能体只有两个业务子智能体
        task_tools = [
            t for node in agent.nodes.values()
            for t in getattr(getattr(node, "bound", None), "tools_by_name", {}).values()
            if t.name == "task"
        ]
        self.assertEqual(len(task_tools), 1)
        # 只截取 "Available agent types" 与 "Specify subagent_type" 之间的可选列表;
        # 模板尾部的使用说明里有静态的 "general-purpose" 字样,不属于实际列表
        listing = task_tools[0].description.split("Available agent types")[1].split("Specify subagent_type")[0]
        self.assertIn("retriever", listing)
        self.assertIn("figure_checker", listing)
        self.assertNotIn("general-purpose", listing)

    def test_isolated_context_and_result_is_last_text_only(self):
        """子智能体只收到自己的 system + task description;回传只有最后一条文本。"""
        agent, _main, sub = self._agent(
            [{"tool": "task", "args": {"description": "找X", "subagent_type": "retriever"}},
             "答案 [chunk:c1]"],
            [{"tool": "search_knowledge", "args": {"query": "X"}}, "【要点】- X [chunk:c1]"])
        result = self._ask(agent)
        first = sub.requests[0]
        self.assertEqual([m.type for m in first], ["system", "human"])
        self.assertEqual(str(first[0].content), subagents.RETRIEVER_PROMPT)   # 完整提示词,不含主提示词
        self.assertEqual(str(first[1].content), "找X")                        # 看不到对话历史
        tool_msgs = [m for m in result["messages"] if m.type == "tool"]
        self.assertEqual(tool_msgs[-1].content, "【要点】- X [chunk:c1]")     # 工具原始 JSON 不回传
        self.assertEqual(self.tracker["chunks"], {"c1"})                      # 但 tracker 副作用已记录

    def test_stream_does_not_leak_subagent_tokens(self):
        """不加 subgraphs 的 messages 流里只有主智能体正文(现有 _stream_impl 不变即安全)。"""
        agent, _main, _sub = self._agent(
            [{"tool": "task", "args": {"description": "找X", "subagent_type": "retriever"}},
             "最终答案 [chunk:c1]"],
            [{"tool": "search_knowledge", "args": {"query": "X"}}, "子智能体内部结论"])
        texts = []
        for chunk, _meta in agent.stream({"messages": [{"role": "user", "content": "问题"}]},
                                         stream_mode="messages", config={"recursion_limit": 40}):
            if getattr(chunk, "type", None) in ("ai", "AIMessageChunk") and not chunk.tool_call_chunks:
                texts.append(chunk.content)
        joined = "".join(texts)
        self.assertEqual(joined, "最终答案 [chunk:c1]")
        self.assertNotIn("子智能体内部结论", joined)

    def test_subagent_loop_is_bounded_by_model_call_limit(self):
        """父级 recursion_limit 管不到子智能体;ModelCallLimitMiddleware 恰好截断在 run_limit。"""
        agent, _main, sub = self._agent(
            [{"tool": "task", "args": {"description": "找X", "subagent_type": "retriever"}},
             "没有找到可靠依据"],
            [{"tool": "search_knowledge", "args": {"query": "x"}}],   # 永远调用工具
            sub_repeat_last=True,
            retriever_max_calls=5)
        result = self._ask(agent)
        self.assertEqual(sub.calls_made, 5)
        tool_msgs = [m for m in result["messages"] if m.type == "tool"]
        self.assertIn("Model call limits exceeded", tool_msgs[-1].content)

    def test_figure_checker_roundtrip_records_image_id(self):
        """figure_checker 调 inspect_image,图片 ID 写入 tracker["images"]。"""
        agent, _main, _sub = self._agent(
            [{"tool": "task",
              "args": {"description": "核对 occ_1 的箭头方向", "subagent_type": "figure_checker"}},
             "结论 [image:occ_1]"],
            [{"tool": "inspect_image", "args": {"image_occurrence_id": "occ_1"}},
             "【核验结果】- [image:occ_1] 箭头向右"])
        self._ask(agent)
        self.assertEqual(self.tracker["images"], {"occ_1"})


class FlagAndPromptTest(unittest.TestCase):
    """开关默认关闭 = 升级前行为;提示词拆分不改动单智能体契约(01 §0.2/§4.2)。"""

    def test_single_agent_prompt_is_unchanged(self):
        self.assertEqual(hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                         "178648324be48d8754340414db192ba2822d8510d150a4cc5ff045f1969c3f88")

    def test_delegating_prompt_shares_citation_rules(self):
        from deephoto.agent.qa import CITATION_AND_FORMAT_RULES
        self.assertTrue(subagents.DELEGATING_SYSTEM_PROMPT.endswith(CITATION_AND_FORMAT_RULES))

    def test_flag_off_by_default(self):
        qa = QAService(SimpleNamespace(db_path=Path("x")),
                       KnowledgeService(store=None, index_service=None))
        self.assertFalse(qa._delegating())
        self.assertEqual(qa._run_kwargs(), {})        # 关闭时不传 config

    def test_flag_on_sets_recursion_limit(self):
        qa = QAService(
            SimpleNamespace(db_path=Path("x"), qa_subagents_enabled=True,
                            qa_main_recursion_limit=25),
            KnowledgeService(store=None, index_service=None))
        self.assertEqual(qa._run_kwargs(), {"config": {"recursion_limit": 25}})


class EndToEndAssembleTest(unittest.TestCase):
    """委派模式下仍由 _assemble 校验引用:子智能体/主智能体编造的 ID 被丢弃。"""

    def setUp(self):
        subagents.register_harness("fake-k3")
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.ctx = AuthContext(tenant_id="ta", user_id="u")
        doc = repo.insert_document(self.conn, tenant_id="ta", owner_id="u", filename="a.pdf",
                                   source_object_key="k", sha256="0" * 64, ingestion_version="v1")
        repo.update_document_status(self.conn, doc, "ready", page_count=5)
        self.chunk = repo.insert_chunk(
            self.conn, tenant_id="ta", document_id=doc, ingestion_version="v1", section=None,
            text="内容", page_start=3, page_end=3, paragraph_ids=["p1"],
            referenced_image_ids=[], nearby_image_ids=[])
        self.qa = QAService(
            SimpleNamespace(db_path=self.db_path, chat_model="fake-k3", qa_subagents_enabled=True),
            KnowledgeService(store=None, index_service=None))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_fabricated_id_from_brief_is_dropped(self):
        tracker = {"chunks": {self.chunk}, "images": set()}      # 工具只提供过真实 chunk
        text = f"事实 [chunk:{self.chunk}] 与编造 [chunk:c_fake]"
        result = self.qa._assemble(self.conn, self.ctx, text, "问题", tracker, set(), set())
        self.assertEqual([c["chunk_id"] for c in result["citations"]], [self.chunk])

    def test_service_stream_end_to_end_with_inherited_model(self):
        """走真实 QAService._build_agent:子智能体不指定 model,继承主模型(同一实例,脚本按调用顺序消费)。"""
        tracker = {"chunks": set(), "images": set()}
        tools = _fake_tools(tracker, self.chunk)
        self.qa._make_tools = lambda ctx: (tools, tracker)
        self.qa._chat_model = _fake_model([
            {"tool": "task", "args": {"description": "找内容", "subagent_type": "retriever"}},  # 主
            {"tool": "search_knowledge", "args": {"query": "内容"}},                            # 子
            "【要点】- 内容 [chunk:%s]" % self.chunk,                                            # 子(最终)
            "根据文档,内容如下 [chunk:%s],另有编造 [chunk:c_fake]。" % self.chunk,              # 主(最终)
        ])
        events = list(self.qa.answer_stream(self.ctx, "问题"))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[-1], "done")
        done = events[-1]
        self.assertEqual([c["chunk_id"] for c in done["citations"]], [self.chunk])  # 编造 ID 被丢弃
        streamed = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertNotIn("【要点】", streamed)                                       # 子智能体文本不外泄


if __name__ == "__main__":
    unittest.main()
