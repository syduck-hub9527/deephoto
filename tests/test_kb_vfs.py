"""知识库虚拟文件系统(02-backends-and-knowledge-vfs)离线测试:真实 sqlite + 假模型,不碰付费 API。

注意:register_harness 是进程内全局且 excluded_tools 只增不减,所以每种 (delegating, kb_vfs) 组合
使用各自独立的假模型名,避免与 01 的 "fake-k3"(delegating, 无 kb)互相污染。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel

from deephoto import repo
from deephoto.agent import harness, subagents
from deephoto.agent.kb_vfs import KB_ROUTE, KnowledgeVFS
from deephoto.agent.knowledge import KnowledgeService
from deephoto.agent.qa import (CITATION_AND_FORMAT_RULES, KB_USAGE_RULES, SYSTEM_PROMPT,
                               SYSTEM_PROMPT_KB, QAService)
from deephoto.db import connect, init_db
from deephoto.security import AuthContext


class _Base(unittest.TestCase):
    """两个租户:ta 有文档 A(两块,第一块关联一张图)与文档 Q(排队中,不可见);tb 有文档 B。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.ctx = AuthContext(tenant_id="ta", user_id="u")
        self.doc_a = self._doc("ta", "a.pdf", "ready")
        self.doc_q = self._doc("ta", "queued.pdf", "queued")
        self.doc_b = self._doc("tb", "b.pdf", "ready")
        asset = repo.get_or_create_asset(self.conn, tenant_id="ta", sha256="i" * 64,
                                         object_key="img", width=10, height=10, mime_type="image/png")
        self.occ = repo.insert_occurrence(
            self.conn, tenant_id="ta", document_id=self.doc_a, ingestion_version="v1",
            image_asset_id=asset, page_number=2, bbox=None, figure_number="3",
            caption="图3 反应路径", extraction_method="embedded_bitmap", needs_review=False)
        repo.update_occurrence_description(
            self.conn, self.occ, visible_summary="一条向右的箭头", visible_labels=["A", "B"],
            caption="", context_summary="", uncertain_details=[], description_model="t")
        self.c1 = self._chunk("ta", self.doc_a, "绪论", "第一段\n第二段含 ΔG=-RT ln K 公式\n第三段", 1,
                              refs=[self.occ])
        self.c2 = self._chunk("ta", self.doc_a, "方法", "采用 BM25 与向量融合\n温度 37°C", 2)
        self.cq = self._chunk("ta", self.doc_q, None, "排队文档里的 ΔG", 1)
        self.cb = self._chunk("tb", self.doc_b, "机密", "租户B的秘密 ΔG", 1)
        self.conn.commit()
        self.tracker = {"chunks": set(), "images": set()}
        self.vfs = self._vfs()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _doc(self, tenant, name, status):
        d = repo.insert_document(self.conn, tenant_id=tenant, owner_id="u", filename=name,
                                 source_object_key="k", sha256=name.ljust(64, "0"), ingestion_version="v1")
        repo.update_document_status(self.conn, d, status, page_count=9)
        return d

    def _chunk(self, tenant, doc, section, text, page, refs=()):
        return repo.insert_chunk(
            self.conn, tenant_id=tenant, document_id=doc, ingestion_version="v1", section=section,
            text=text, page_start=page, page_end=page, paragraph_ids=[],
            referenced_image_ids=list(refs), nearby_image_ids=[])

    def _vfs(self, ctx=None, **kw):
        return KnowledgeVFS(KnowledgeService(store=None, index_service=None), ctx or self.ctx,
                            lambda: connect(self.db_path), self.tracker, **kw)


class VfsBackendTest(_Base):
    def test_tenant_isolation_and_ready_only(self):
        """别的租户的文档、未 ready 的文档在 ls/read/grep/glob 里都不存在。"""
        names = [e["path"] for e in self.vfs.ls("/").entries]
        self.assertEqual(sorted(names), sorted(["/index.md", f"/{self.doc_a}/"]))
        for hidden in (self.doc_b, self.doc_q):
            self.assertIsNotNone(self.vfs.read(f"/{hidden}/content.md").error)
            self.assertIsNotNone(self.vfs.ls(f"/{hidden}").error)
        paths = {m["path"] for m in self.vfs.glob("**/*.md").matches}
        self.assertEqual(paths, {"/index.md", f"/{self.doc_a}/content.md", f"/{self.doc_a}/figures.md"})
        hits = self.vfs.grep("ΔG", "/").matches
        self.assertEqual({m["path"] for m in hits}, {f"/{self.doc_a}/content.md"})
        # 另一个租户视角:只看到自己的文档
        other = self._vfs(AuthContext(tenant_id="tb", user_id="u"))
        self.assertEqual([e["path"] for e in other.ls("/").entries if e["is_dir"]], [f"/{self.doc_b}/"])
        self.assertIn("租户B的秘密", other.read(f"/{self.doc_b}/content.md").file_data["content"])
        self.assertNotIn("租户B的秘密", self.vfs.read("/index.md").file_data["content"])

    def test_layout_has_chunk_headers_with_ids_and_locator(self):
        content = self.vfs.read(f"/{self.doc_a}/content.md").file_data["content"]
        self.assertIn(f"### [chunk:{self.c1}] p.1", content)
        self.assertIn(f"### [chunk:{self.c2}] p.2", content)
        self.assertIn(f"关联图片:[image:{self.occ}](图 3)", content)
        self.assertLess(content.index(self.c1), content.index(self.c2))      # 阅读顺序
        index = self.vfs.read("/index.md").file_data["content"]
        self.assertIn(f"{self.doc_a} · a.pdf · 格式 pdf · 共 9 页", index)

    def test_read_snaps_back_to_chunk_header(self):
        """从块中间开始读,窗口回退到块头,start_line 如实反映,模型一定同时拿到可引用 ID。"""
        r = self.vfs.read(f"/{self.doc_a}/content.md", offset=5, limit=2)   # 落在 c1 的第二行正文
        self.assertTrue(r.file_data["content"].startswith(f"### [chunk:{self.c1}]"))
        self.assertEqual(r.start_line, 4)               # 块头是第 4 行(1 起)
        self.assertGreaterEqual(r.end_line, 6)          # 仍覆盖请求的 offset+limit
        self.assertEqual(r.next_offset, r.end_line)

    def test_read_bounds(self):
        path = f"/{self.doc_a}/content.md"
        self.assertTrue(self.vfs.read(path, offset=0, limit=0).no_lines_requested)
        self.assertIn("exceeds", self.vfs.read(path, offset=999).error)
        self.assertEqual(self.vfs.read(path, offset=-5, limit=1).start_line, 1)   # 负 offset 当 0
        whole = self.vfs.read(path)
        self.assertIsNone(whole.next_offset)            # 读完无下一页
        self.assertEqual(whole.end_line, whole.total_lines)

    def test_tracker_registers_only_what_was_returned(self):
        path = f"/{self.doc_a}/content.md"
        self.vfs.read(path, offset=4, limit=1)          # 只在 c1 内
        self.assertEqual(self.tracker["chunks"], {self.c1})
        self.assertEqual(self.tracker["images"], {self.occ})    # c1 关联图随块一起登记
        self.vfs.grep("BM25", path)
        self.assertEqual(self.tracker["chunks"], {self.c1, self.c2})
        self.vfs.grep("绝对不存在的词", path)
        self.assertEqual(self.tracker["chunks"], {self.c1, self.c2})

    def test_grep_is_literal_scoped_and_capped(self):
        path = f"/{self.doc_a}/content.md"
        res = self.vfs.grep("-RT ln K", path)           # 含正则元字符也按字面匹配
        self.assertEqual([m["line"] for m in res.matches], [6])
        self.assertEqual(self.vfs.grep("bm25", path).matches, [])         # 区分大小写
        self.assertEqual(self.vfs.grep("温度", f"/{self.doc_a}/figures.md").matches, [])
        capped = self._vfs(grep_max_docs=0).grep("ΔG", "/")
        self.assertIn("超过单次 grep 上限", capped.error)
        # 限定到单文档不受整库上限影响
        self.assertTrue(self._vfs(grep_max_docs=1).grep("ΔG", path).matches)

    def test_figures_listing_registers_image(self):
        text = self.vfs.read(f"/{self.doc_a}/figures.md").file_data["content"]
        self.assertIn(f"### [image:{self.occ}] 图 3 · p.2", text)
        self.assertIn("一条向右的箭头", text)
        self.assertIn("图中可见文字:A、B", text)
        self.assertEqual(self.tracker["images"], {self.occ})
        self.assertEqual(self.tracker["chunks"], set())      # 图清单不涉及正文块

    def test_everything_is_read_only(self):
        self.assertIsNotNone(self.vfs.write("/x.md", "a").error)
        self.assertIsNotNone(self.vfs.edit(f"/{self.doc_a}/content.md", "第一段", "改").error)
        self.assertIsNotNone(self.vfs.delete(f"/{self.doc_a}/content.md").error)
        self.assertEqual(self.vfs.upload_files([("/x", b"1")])[0].error, "permission_denied")
        self.assertIn("第一段", self.vfs.read(f"/{self.doc_a}/content.md").file_data["content"])

    def test_path_normalization_cannot_escape(self):
        self.assertIsNotNone(self.vfs.read("/../../etc/passwd").error)
        self.assertIsNotNone(self.vfs.read(f"/{self.doc_a}/../{self.doc_b}/content.md").error)
        self.assertIsNotNone(self.vfs.read(f"/{self.doc_a}/content.md/extra").error)
        self.assertIsNotNone(self.vfs.read(f"/{self.doc_a}").error)         # 目录不是文件

    def test_ls_directory_reports_sizes(self):
        entries = {e["path"]: e for e in self.vfs.ls(f"/{self.doc_a}").entries}
        self.assertGreater(entries[f"/{self.doc_a}/content.md"]["size"], 0)
        self.assertEqual(set(entries), {f"/{self.doc_a}/content.md", f"/{self.doc_a}/figures.md"})

    def test_upstream_private_helpers_still_exist(self):
        """kb_vfs 依赖 deepagents 的少量非公开 helper;升级 deepagents 时这条先红,提示核对语义。"""
        from deepagents.backends import utils
        for name in ("_glob_search_files", "grep_matches_from_files", "normalize_read_bounds",
                     "create_file_data", "InvalidGlobPatternError"):
            self.assertTrue(hasattr(utils, name), name)


class HarnessTest(unittest.TestCase):
    def test_excluded_sets(self):
        self.assertEqual(harness.excluded_tools(delegating=False, kb_vfs=False), frozenset())
        self.assertEqual(harness.excluded_tools(delegating=True, kb_vfs=False), harness.ALL_FS_TOOLS)
        for delegating in (False, True):
            ex = harness.excluded_tools(delegating=delegating, kb_vfs=True)
            self.assertEqual(ex, harness.WRITE_TOOLS)
            self.assertFalse(ex & harness.READ_TOOLS)

    def test_reregistering_with_other_flags_is_refused(self):
        """并集无法撤销:同一进程里同一 key 换配置必须报错,而不是悄悄保留旧的排除。"""
        harness.register_harness("guard-k3", delegating=True, kb_vfs=False)
        harness.register_harness("guard-k3", delegating=True, kb_vfs=False)    # 幂等
        with self.assertRaises(RuntimeError):
            harness.register_harness("guard-k3", delegating=True, kb_vfs=True)

    def test_single_without_kb_registers_nothing(self):
        """单智能体且无 kb:升级前行为——内置文件工具一个都不被排除(用真实 agent 看模型可见工具)。"""
        from deepagents import create_deep_agent
        harness.register_harness("noop-k3", delegating=False, kb_vfs=False)
        model = _fake_model(["好"], "noop-k3")
        create_deep_agent(model=model, tools=[]).invoke({"messages": [{"role": "user", "content": "q"}]})
        self.assertTrue(harness.ALL_FS_TOOLS - {"execute"} <= set(model.bound_tool_names[0]))


class PromptAndFlagTest(unittest.TestCase):
    def test_kb_prompt_layout(self):
        self.assertNotIn("/kb/", SYSTEM_PROMPT)               # 关闭时单智能体提示词不含 kb 说明
        self.assertIn(KB_USAGE_RULES, SYSTEM_PROMPT_KB)
        self.assertTrue(SYSTEM_PROMPT_KB.endswith(CITATION_AND_FORMAT_RULES))
        self.assertIn("output_mode=\"content\"", KB_USAGE_RULES)

    def test_retriever_kb_prompt_keeps_format_contract_last(self):
        self.assertIn(KB_USAGE_RULES, subagents.RETRIEVER_KB_PROMPT)
        self.assertTrue(subagents.RETRIEVER_KB_PROMPT.endswith("【缺口】\n- 没找到或不确定的内容(没有写“无”)"))
        self.assertEqual(subagents.RETRIEVER_PROMPT.replace("4. 最终回复", "4. 最终回复"),
                         subagents.RETRIEVER_PROMPT)

    def test_flag_off_means_no_backend_kwargs(self):
        qa = QAService(SimpleNamespace(db_path=Path("x")), KnowledgeService(None, None))
        self.assertFalse(qa._kb_vfs())
        self.assertEqual(qa._agent_kwargs(AuthContext(tenant_id="t", user_id="u"), {}), {})


def _fake_model(script, name, **kw):
    return ScriptedFakeChatModel(script=script, model_name=name, ls_provider="openai", **kw)


class AgentIntegrationTest(_Base):
    """经真实 QAService + deepagents:工具可见性、租户隔离、引用校验、写入被拒、转存路径可写。"""

    def _qa(self, model_name, *, delegating, script, **kw):
        qa = QAService(
            SimpleNamespace(db_path=self.db_path, chat_model=model_name, qa_kb_vfs_enabled=True,
                            qa_subagents_enabled=delegating, **kw),
            KnowledgeService(store=None, index_service=None))
        qa._chat_model = _fake_model(script, model_name)
        return qa

    def test_single_agent_tool_visibility(self):
        qa = self._qa("kb-single-k3", delegating=False, script=["答案"])
        list(qa.answer_stream(self.ctx, "问题"))
        visible = set(qa._chat_model.bound_tool_names[-1])
        self.assertEqual(visible, {"search_knowledge", "read_chunk", "inspect_image",
                                   "ls", "read_file", "glob", "grep", "task"})
        self.assertFalse(visible & harness.WRITE_TOOLS)

    def test_single_agent_end_to_end_grep_then_read_then_cite(self):
        script = [
            {"tool": "grep", "args": {"pattern": "BM25", "path": f"/kb/{self.doc_a}/content.md",
                                      "output_mode": "content"}},
            {"tool": "read_file", "args": {"file_path": f"/kb/{self.doc_a}/content.md",
                                           "offset": 8, "limit": 2}},
            f"检索采用 BM25 与向量融合 [chunk:{self.c2}];另编造 [chunk:{self.cb}] 与 [chunk:c_fake]。",
        ]
        qa = self._qa("kb-single-k3", delegating=False, script=script)
        events = list(qa.answer_stream(self.ctx, "用了什么检索方法?"))
        done = events[-1]
        self.assertEqual(done["type"], "done")
        # 真实块被接受;别的租户的块 ID、编造 ID 都被丢弃
        self.assertEqual([c["chunk_id"] for c in done["citations"]], [self.c2])
        tool_text = "\n".join(str(m.content) for m in qa._chat_model.requests[-1] if m.type == "tool")
        self.assertIn(f"[chunk:{self.c2}]", tool_text)           # 模型确实读到了块头里的 ID
        self.assertNotIn("租户B", tool_text)

    def test_write_attempt_is_rejected_and_nothing_changes(self):
        script = [{"tool": "write_file", "args": {"file_path": f"/kb/{self.doc_a}/content.md", "content": "x"}},
                  "好"]
        qa = self._qa("kb-single-k3", delegating=False, script=script)
        list(qa.answer_stream(self.ctx, "q"))
        tool_msgs = [m for m in qa._chat_model.requests[-1] if m.type == "tool"]
        self.assertIn("not available", tool_msgs[0].content)
        self.assertIn("第一段", self.vfs.read(f"/{self.doc_a}/content.md").file_data["content"])

    def test_delegating_visibility_main_task_only_retriever_gets_kb(self):
        qa = self._qa("kb-deleg-k3", delegating=True, script=["x"])
        tools, tracker = qa._make_tools(self.ctx)
        agent = qa._build_agent(tools, **qa._agent_kwargs(self.ctx, tracker))
        main = qa._chat_model
        # 三个模型视角分开断言:主智能体、retriever、figure_checker 各给独立假模型
        retr = _fake_model([{"tool": "ls", "args": {"path": "/kb/"}}, "【要点】- 无"], "kb-deleg-k3")
        fig = _fake_model(["【核验结果】- 无"], "kb-deleg-k3")
        specs = subagents.build_subagent_specs(tools, kb_vfs=True)
        specs[0]["model"], specs[1]["model"] = retr, fig
        from deepagents import create_deep_agent
        from deepagents.backends import CompositeBackend, StateBackend
        backend = CompositeBackend(default=StateBackend(), routes={KB_ROUTE: self.vfs})
        main = _fake_model([
            {"tool": "task", "args": {"description": "找", "subagent_type": "retriever"}},
            {"tool": "task", "args": {"description": "看图 occ", "subagent_type": "figure_checker"}},
            "结束"], "kb-deleg-k3")
        agent = create_deep_agent(
            model=main, tools=[], system_prompt=subagents.DELEGATING_SYSTEM_PROMPT, subagents=specs,
            backend=backend, middleware=[harness.hide_tools_middleware(harness.READ_TOOLS)])
        agent.invoke({"messages": [{"role": "user", "content": "q"}]}, config={"recursion_limit": 40})
        self.assertEqual(main.bound_tool_names[0], ["task"])
        self.assertEqual(set(retr.bound_tool_names[0]),
                         {"search_knowledge", "read_chunk", "ls", "read_file", "glob", "grep"})
        self.assertEqual(fig.bound_tool_names[0], ["inspect_image"])
        # retriever 的 ls 走到了 /kb/:提示词里的 KB 说明与工具实际可用一致
        sys_prompt = str(retr.requests[0][0].content)
        self.assertIn("/kb/", sys_prompt)
        # 租户:retriever 的 ls 结果只含本租户文档
        listing = [m for m in retr.requests[-1] if m.type == "tool"][0].content
        self.assertIn(self.doc_a, listing)
        self.assertNotIn(self.doc_b, listing)

    def test_large_tool_result_eviction_still_works_with_composite(self):
        """默认后端是可写的临时 StateBackend:超大工具结果转存到 /large_tool_results/ 不报错。"""
        def big_tool(x: str = "") -> str:
            """返回超大文本。"""
            return "超长内容" * 40000
        from deepagents import create_deep_agent
        from deepagents.backends import CompositeBackend, StateBackend
        harness.register_harness("kb-evict-k3", delegating=False, kb_vfs=True)
        backend = CompositeBackend(default=StateBackend(), routes={KB_ROUTE: self.vfs})
        model = _fake_model([{"tool": "big_tool", "args": {"x": "1"}}, "完"], "kb-evict-k3")
        agent = create_deep_agent(model=model, tools=[big_tool], backend=backend)
        result = agent.invoke({"messages": [{"role": "user", "content": "q"}]})
        self.assertTrue(any(k.startswith("/large_tool_results/") for k in result["files"]))
        stub = [m for m in result["messages"] if m.type == "tool"][0].content
        self.assertLess(len(stub), 5000)


if __name__ == "__main__":
    unittest.main()
