"""06 人工审批(HITL)离线测试:假模型 + 真实 SQLite/LangGraph,不访问付费 API。

覆盖(md文档/files/06-human-in-the-loop.md §9.1):
- 闸门:N=0 全询问;N=2 前两次自动第三次起询问;同一 tool_call_id 结果稳定;并发计数正确
- approval_items / validate_decisions / build_resume_value 纯函数
- describe:图号页码、缺图退化、跨租户不泄露元数据、不含图片字节
- 服务级挂起/恢复/状态机/超时/校验:见文件后半部分
"""

from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel
from test_kb_vfs import _Base

from langchain_core.messages import AIMessage, HumanMessage

from deephoto import repo
from deephoto.agent.hitl import (ApprovalError, ImageApprovalGate, approval_items,
                                 build_resume_value, describe_image_request, validate_decisions)
from deephoto.agent.persistence import SessionError
from deephoto.agent.qa import QAService
from deephoto.db import connect


def _req(call_id: str):
    return SimpleNamespace(tool_call={"id": call_id})


class GateTest(unittest.TestCase):
    """§9.1 闸门:预算、幂等、并发。"""

    def test_zero_budget_asks_every_time(self):
        gate = ImageApprovalGate(0)
        self.assertTrue(gate.when(_req("c1")))
        self.assertTrue(gate.when(_req("c2")))

    def test_first_n_auto_then_ask(self):
        gate = ImageApprovalGate(2)
        self.assertFalse(gate.when(_req("c1")))
        self.assertFalse(gate.when(_req("c2")))
        self.assertTrue(gate.when(_req("c3")))
        self.assertTrue(gate.when(_req("c4")))
        self.assertEqual(gate.snapshot(), {"c1": "auto", "c2": "auto", "c3": "ask", "c4": "ask"})

    def test_same_call_id_decision_is_stable(self):
        """U2:恢复重放时同一 tool_call_id 重复求值,结论不能变、auto 计数不能翻倍。"""
        gate = ImageApprovalGate(1)
        self.assertFalse(gate.when(_req("c1")))
        self.assertFalse(gate.when(_req("c1")))          # 重放不翻倍
        self.assertTrue(gate.when(_req("c2")))           # 预算只被 c1 用掉一次
        gate2 = ImageApprovalGate(1, decided=gate.snapshot())
        self.assertFalse(gate2.when(_req("c1")))         # 恢复的 decided 直接复用
        self.assertTrue(gate2.when(_req("c2")))

    def test_concurrent_when_counts_decisions_not_results(self):
        """U1:并行子智能体并发调用 when;N=2 时恰好两个 auto,其余全 ask。"""
        gate = ImageApprovalGate(2)
        results: dict[str, bool] = {}
        threads = [threading.Thread(target=lambda i=i: results.setdefault(f"c{i}", gate.when(_req(f"c{i}"))))
                   for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results.values()), [False, False] + [True] * 8)
        self.assertEqual(len(gate.snapshot()), 10)


class DescribeTest(_Base):
    def test_full_label_with_figure_page_and_caption(self):
        text = describe_image_request(self.db_path, self.ctx,
                                      {"args": {"image_occurrence_id": self.occ}})
        self.assertEqual(text, f"查看原图 {self.occ}(图 3, 第 2 页, 图3 反应路径)")

    def test_unknown_or_cross_tenant_falls_back_without_metadata(self):
        self.assertEqual(describe_image_request(self.db_path, self.ctx,
                                                {"args": {"image_occurrence_id": "occ_none"}}),
                         "查看原图 occ_none")
        other_tenant = SimpleNamespace(tenant_id="tb", user_id="u")
        text = describe_image_request(self.db_path, other_tenant,
                                      {"args": {"image_occurrence_id": self.occ}})
        self.assertEqual(text, f"查看原图 {self.occ}")     # 不泄露 ta 的图号/页码
        self.assertEqual(describe_image_request(self.db_path, self.ctx, {"args": {}}),
                         "查看原图 (未知图片)")

    def test_description_never_carries_image_bytes(self):
        # 给资产塞入伪二进制,描述里不得出现;只含文本元数据
        text = describe_image_request(self.db_path, self.ctx,
                                      {"args": {"image_occurrence_id": self.occ}})
        self.assertNotIn("base64", text)
        self.assertLess(len(text), 200)


class ApprovalItemsTest(unittest.TestCase):
    def test_expands_interrupts_in_order(self):
        interrupts = [
            SimpleNamespace(id="int-1", value={"action_requests": [
                {"name": "inspect_image", "args": {"image_occurrence_id": "o1"}, "description": "d1"},
                {"name": "inspect_image", "args": {"image_occurrence_id": "o2"}, "description": "d2"},
            ]}),
            SimpleNamespace(id="int-2", value={"action_requests": [
                {"name": "inspect_image", "args": {"image_occurrence_id": "o3"}, "description": "d3"},
            ]}),
        ]
        items = approval_items(interrupts)
        self.assertEqual([i["approval_id"] for i in items], ["ap_1", "ap_2", "ap_3"])
        self.assertEqual([(i["interrupt_id"], i["index"]) for i in items],
                         [("int-1", 0), ("int-1", 1), ("int-2", 0)])
        self.assertEqual(items[0]["tool"], "inspect_image")
        self.assertEqual(items[1]["args"], {"image_occurrence_id": "o2"})


class ValidateDecisionsTest(unittest.TestCase):
    PENDING = [
        {"approval_id": "ap_1", "interrupt_id": "int-1", "index": 0},
        {"approval_id": "ap_2", "interrupt_id": "int-1", "index": 1},
    ]

    def test_ok_normalizes_in_pending_order(self):
        out = validate_decisions(self.PENDING, [
            {"approval_id": "ap_2", "type": "reject", "message": "不用看"},
            {"approval_id": "ap_1", "type": "approve"},
        ])
        self.assertEqual([(d["interrupt_id"], d["index"], d["type"]) for d in out],
                         [("int-1", 0, "approve"), ("int-1", 1, "reject")])
        self.assertEqual(out[1]["message"], "不用看")
        self.assertNotIn("message", out[0])

    def test_missing_duplicate_extra_bad_type_message_rules(self):
        with self.assertRaises(ApprovalError):           # 缺项
            validate_decisions(self.PENDING, [{"approval_id": "ap_1", "type": "approve"}])
        with self.assertRaises(ApprovalError):           # 重复
            validate_decisions(self.PENDING, [{"approval_id": "ap_1", "type": "approve"},
                                              {"approval_id": "ap_1", "type": "approve"}])
        with self.assertRaises(ApprovalError):           # 多余
            validate_decisions(self.PENDING, [{"approval_id": "ap_1", "type": "approve"},
                                              {"approval_id": "ap_2", "type": "approve"},
                                              {"approval_id": "ap_9", "type": "approve"}])
        with self.assertRaises(ApprovalError):           # 非法 type(edit/respond 不开放)
            validate_decisions(self.PENDING, [{"approval_id": "ap_1", "type": "edit"},
                                              {"approval_id": "ap_2", "type": "approve"}])
        with self.assertRaises(ApprovalError):           # approve 带 message
            validate_decisions(self.PENDING, [{"approval_id": "ap_1", "type": "approve", "message": "x"},
                                              {"approval_id": "ap_2", "type": "approve"}])
        with self.assertRaises(ApprovalError):           # message 超长
            validate_decisions(self.PENDING, [{"approval_id": "ap_1", "type": "approve"},
                                              {"approval_id": "ap_2", "type": "reject", "message": "x" * 201}])
        with self.assertRaises(ApprovalError):           # decisions 不是数组
            validate_decisions(self.PENDING, {"approval_id": "ap_1"})

    def test_build_resume_value_single_vs_multi_interrupt(self):
        one = validate_decisions(self.PENDING[:1], [{"approval_id": "ap_1", "type": "approve"}])
        self.assertEqual(build_resume_value(one), {"decisions": [{"type": "approve"}]})
        pending2 = self.PENDING + [{"approval_id": "ap_3", "interrupt_id": "int-2", "index": 0}]
        multi = validate_decisions(pending2, [{"approval_id": "ap_1", "type": "approve"},
                                              {"approval_id": "ap_2", "type": "reject"},
                                              {"approval_id": "ap_3", "type": "approve"}])
        self.assertEqual(build_resume_value(multi),
                         {"int-1": {"decisions": [{"type": "approve"}, {"type": "reject"}]},
                          "int-2": {"decisions": [{"type": "approve"}]}})
        with self.assertRaises(ApprovalError):
            build_resume_value([])


class _ServiceCase(_Base):
    """真实 SQLite + 假模型 + 真实 LangGraph 图;HITL 默认预算 0(每次都询问)。"""

    def setUp(self):
        super().setUp()
        self.services = []
        self.image_calls = []

    def tearDown(self):
        for qa in self.services:
            qa.close()
        super().tearDown()

    def _qa(self, *, budget=0, delegating=False, middleware=False, timeout=600, persistence=True):
        knowledge = self._vfs()._knowledge
        self.image_calls = []

        def image_blocks(conn, ctx, occ):
            self.image_calls.append(occ)
            return [{"type": "text", "text": f"[image:{occ}]"},
                    {"type": "image", "base64": "PIXELS", "mime_type": "image/png"}]
        knowledge.image_content_blocks = image_blocks
        knowledge.search = lambda *args: {"chunks": [{"chunk_id": self.c1, "text": "正文"}], "images": []}
        qa = QAService(SimpleNamespace(
            db_path=self.db_path,
            qa_persistence_enabled=persistence, qa_middleware_enabled=middleware,
            qa_subagents_enabled=delegating, qa_kb_vfs_enabled=False, qa_skills_enabled=False,
            qa_hitl_enabled=True, qa_hitl_auto_approve_images=budget,
            qa_hitl_timeout_seconds=timeout,
            qa_main_max_model_calls=12, qa_main_recursion_limit=50,
            chat_model=f"fake-hitl-{int(delegating)}-{int(middleware)}-{budget}-{timeout}"), knowledge)
        self.services.append(qa)
        return qa

    def _model(self, qa, script):
        qa._chat_model = ScriptedFakeChatModel(script=script, model_name=qa.settings.chat_model,
                                               ls_provider="openai")
        return qa._chat_model

    def _metadata(self, qa, sid):
        ns, _tid = qa._sessions()._keys(self.ctx, sid)
        return qa._sessions().store.get(ns, sid).value

    def _backdate_expiry(self, qa, sid):
        """把挂起元数据的 expires_at 改到过去,模拟超时(惰性触发,不设后台任务)。"""
        ns, _tid = qa._sessions()._keys(self.ctx, sid)
        meta = qa._sessions().store.get(ns, sid).value
        qa._sessions().store.put(ns, sid, {**meta, "expires_at": "2020-01-01T00:00:00+00:00"})

    def _park_first_turn(self, qa):
        """第一轮:模型要看图,预算 0 -> 挂起。返回 (session_id, 挂起事件/结果)。"""
        model = self._model(qa, [
            {"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},
            f"看到了图 [image:{self.occ}]"])
        events = list(qa.answer_stream(self.ctx, "q1", self.doc_a))
        self.assertEqual(events[-1]["type"], "approval_required")
        self.assertEqual(events[-1]["session_status"], "awaiting_approval")
        self.assertEqual(self.image_calls, [])                    # F1:批准前工具不执行
        sid = events[-1]["session_id"]
        return sid, events[-1], model


class HitlParkResumeTest(_ServiceCase):
    """§9.1:挂起检测、状态机、批准/拒绝恢复、一轮多道闸门。"""

    def test_budget_auto_approves_without_parking(self):
        qa = self._qa(budget=2)
        model = self._model(qa, [{"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},
                                 f"看到了图 [image:{self.occ}]"])
        result = qa.answer(self.conn, self.ctx, "q1", document_id=self.doc_a)
        self.assertEqual(result["answer"], f"看到了图\n\n[image:{self.occ}]")
        self.assertEqual(self.image_calls, [self.occ])            # 自动放行,工具照常执行
        meta = self._metadata(qa, result["session_id"])
        self.assertEqual(meta["status"], "ready")
        self.assertNotIn("approvals", meta)                       # 从未挂起

    def test_non_stream_park_returns_awaiting_without_assemble(self):
        qa = self._qa()
        self._model(qa, [{"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},
                         f"看到了图 [image:{self.occ}]"])
        result = qa.answer(self.conn, self.ctx, "q1", document_id=self.doc_a)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertIsNone(result["answer"])                       # 不组装、不返回答案
        self.assertEqual(result["approvals"][0]["approval_id"], "ap_1")
        self.assertIn("图 3", result["approvals"][0]["description"])
        self.assertNotIn("interrupt_id", result["approvals"][0])  # 内部字段不下发
        self.assertEqual(self.image_calls, [])
        meta = self._metadata(qa, result["session_id"])
        self.assertEqual(meta["status"], "awaiting_approval")     # 不得判 failed
        self.assertEqual(meta["decided"], {"call_1": "ask"})
        self.assertEqual(meta["approvals"][0]["interrupt_id"] is not None, True)
        self.assertTrue(meta["previous"])                         # 超时恢复落点已保存

    def test_approve_resume_executes_once_and_completes(self):
        qa = self._qa()
        sid, parked, model = self._park_first_turn(qa)
        self.assertEqual(model.calls_made, 1)
        events = list(qa.resolve_approvals(self.ctx, sid, [{"approval_id": "ap_1", "type": "approve"}]))
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["session_status"], "ready")
        self.assertEqual(events[-1]["answer"], f"看到了图\n\n[image:{self.occ}]")
        self.assertEqual([i["image_occurrence_id"] for i in events[-1]["images"]], [self.occ])
        self.assertEqual(self.image_calls, [self.occ])            # 批准后工具恰好执行一次
        self.assertEqual(model.calls_made, 2)                     # 恢复本身不调模型,只有一次收尾
        self.assertEqual(self._metadata(qa, sid)["status"], "ready")

    def test_reject_resume_wraps_up_without_image(self):
        qa = self._qa()
        sid, _parked, model = self._park_first_turn(qa)
        events = list(qa.resolve_approvals(
            self.ctx, sid, [{"approval_id": "ap_1", "type": "reject", "message": "不用看图"}]))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["session_status"], "ready")
        self.assertEqual(self.image_calls, [])                    # 拒绝后工具不执行
        self.assertEqual(events[-1]["images"], [])                # 回答里不应有图片
        tool_texts = [str(m.content) for m in model.requests[-1]
                      if m.__class__.__name__ == "ToolMessage"]
        self.assertTrue(any("rejected" in t and "不用看图" in t for t in tool_texts))
        self.assertEqual(self._metadata(qa, sid)["status"], "ready")

    def test_second_gate_in_same_turn_parks_again(self):
        """一轮中可能有多道闸门:恢复后再次挂起,decided 跨挂起累计(预算按轮计算)。"""
        qa = self._qa()
        model = self._model(qa, [
            {"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},
            {"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},
            f"两张都看了 [image:{self.occ}]"])
        first = list(qa.answer_stream(self.ctx, "q1", self.doc_a))
        sid = first[-1]["session_id"]
        second = list(qa.resolve_approvals(self.ctx, sid, [{"approval_id": "ap_1", "type": "approve"}]))
        self.assertEqual(second[-1]["type"], "approval_required")  # 第二道闸门
        self.assertEqual(self.image_calls, [self.occ])
        meta = self._metadata(qa, sid)
        self.assertEqual(meta["status"], "awaiting_approval")
        self.assertEqual(meta["decided"], {"call_1": "ask", "call_2": "ask"})
        third = list(qa.resolve_approvals(self.ctx, sid, [{"approval_id": "ap_1", "type": "approve"}]))
        self.assertEqual(third[-1]["type"], "done")
        self.assertEqual(third[-1]["session_status"], "ready")
        self.assertEqual(self.image_calls, [self.occ, self.occ])
        self.assertEqual(model.calls_made, 3)

    def test_question_while_pending_is_rejected_and_state_untouched(self):
        qa = self._qa()
        sid, _parked, model = self._park_first_turn(qa)
        before = dict(self._metadata(qa, sid))
        pending = qa.approval_pending(self.ctx, sid)
        self.assertEqual(pending["code"], "approval_pending")
        self.assertEqual(pending["approvals"][0]["approval_id"], "ap_1")
        self.assertTrue(pending["expires_at"])
        with self.assertRaisesRegex(SessionError, "等待图片审批"):
            qa.answer(self.conn, self.ctx, "q2", document_id=self.doc_a, session_id=sid)
        events = list(qa.answer_stream(self.ctx, "q2", self.doc_a, session_id=sid))
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("等待图片审批", events[-1]["detail"])
        self.assertEqual(self._metadata(qa, sid), before)         # 状态不变
        self.assertEqual(model.calls_made, 1)                     # 图未被触碰

    def test_invalid_decisions_never_touch_the_graph(self):
        """F7:错误决定会毁掉挂起的线程——所有校验必须在图外完成,失败后线程仍可恢复。"""
        qa = self._qa()
        sid, _parked, model = self._park_first_turn(qa)
        for bad in ([], [{"approval_id": "ap_1"}],                               # 空 / 缺 type
                    [{"approval_id": "ap_2", "type": "approve"}],                # 不存在的 id
                    [{"approval_id": "ap_1", "type": "edit"}],                   # 不开放 edit
                    [{"approval_id": "ap_1", "type": "approve", "message": "x"}]):
            with self.assertRaises(ApprovalError):
                qa.resolve_approvals(self.ctx, sid, bad)
        self.assertEqual(model.calls_made, 1)
        self.assertEqual(self._metadata(qa, sid)["status"], "awaiting_approval")
        events = list(qa.resolve_approvals(self.ctx, sid, [{"approval_id": "ap_1", "type": "approve"}]))
        self.assertEqual(events[-1]["type"], "done")              # 线程仍可正常恢复

    def test_resume_stream_uses_sync_durability(self):
        qa = self._qa()
        sid, _parked, _model = self._park_first_turn(qa)
        seen = []
        original = qa._invoke_kwargs

        def spy(turn=None):
            kwargs = original(turn)
            seen.append(kwargs.get("durability"))
            return kwargs
        qa._invoke_kwargs = spy
        list(qa.resolve_approvals(self.ctx, sid, [{"approval_id": "ap_1", "type": "approve"}]))
        self.assertEqual(seen, ["sync"])                          # 恢复路径复用 sync 落盘

    def test_delegating_mode_parks_inside_figure_checker(self):
        """委派模式:闸门在 figure_checker 子智能体内部生效(F2/F3),恢复后正常完成。"""
        qa = self._qa(delegating=True)
        model = self._model(qa, [
            {"tool": "task", "args": {"subagent_type": "figure_checker",
                                      "description": f"核对 {self.occ}"}},
            {"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},
            "【核验结果】箭头向右", f"图中箭头向右 [image:{self.occ}]"])
        events = list(qa.answer_stream(self.ctx, "q1", self.doc_a))
        self.assertEqual(events[-1]["type"], "approval_required")
        self.assertEqual(self.image_calls, [])
        resumed = list(qa.resolve_approvals(self.ctx, events[-1]["session_id"],
                                            [{"approval_id": "ap_1", "type": "approve"}]))
        self.assertEqual(resumed[-1]["type"], "done")
        self.assertEqual(resumed[-1]["answer"], f"图中箭头向右\n\n[image:{self.occ}]")
        self.assertEqual(self.image_calls, [self.occ])
        self.assertEqual(model.calls_made, 4)                     # 主 task + 子收尾 + 主收尾(+挂起前的子调用)


class HitlTimeoutTest(_ServiceCase):
    """§9.1 超时:惰性自动拒绝,恢复 previous,丢弃的输出不出现。"""

    def test_expired_question_first_auto_rejects_then_answers(self):
        qa = self._qa()
        model = self._model(qa, [
            {"tool": "search_knowledge", "args": {"query": "x"}},
            f"第一问答案 [chunk:{self.c1}]",                       # q1 正常完成(带引用)
            {"tool": "inspect_image", "args": {"image_occurrence_id": self.occ}},  # q2 挂起
            "EXPIRED_DISCARD",                                     # 超时自动拒绝后的收尾(应被丢弃)
            "第三问的答案"])
        first = qa.answer(self.conn, self.ctx, "q1", document_id=self.doc_a)
        sid = first["session_id"]
        parked = list(qa.answer_stream(self.ctx, "q2", self.doc_a, session_id=sid))
        self.assertEqual(parked[-1]["type"], "approval_required")
        before = self._metadata(qa, sid)
        self.assertEqual(before["chunk_ids"], [self.c1])
        self._backdate_expiry(qa, sid)

        result = qa.answer(self.conn, self.ctx, "q3", document_id=self.doc_a, session_id=sid)
        self.assertEqual(result["answer"], "第三问的答案")          # 再按该请求处理
        self.assertNotIn("EXPIRED_DISCARD", json.dumps(result))    # 被丢弃的答案不出现
        self.assertEqual(self.image_calls, [])                     # 超时的看图从未执行
        after = self._metadata(qa, sid)
        self.assertEqual(after["status"], "ready")
        self.assertEqual(after["chunk_ids"], [self.c1])            # 引用列表保持不变
        self.assertNotIn("approvals", after)
        self.assertEqual(model.calls_made, 5)                      # 含一次被丢弃的超时收尾

    def test_approval_request_after_expiry_gets_400(self):
        qa = self._qa()
        sid, _parked, model = self._park_first_turn(qa)
        self._backdate_expiry(qa, sid)
        with self.assertRaisesRegex(ApprovalError, "没有待审批"):
            qa.resolve_approvals(self.ctx, sid, [{"approval_id": "ap_1", "type": "approve"}])
        self.assertEqual(model.calls_made, 2)                      # 自动拒绝跑完一轮
        self.assertEqual(self._metadata(qa, sid)["status"], "ready")


class HitlConfigTest(unittest.TestCase):
    """§9.1 配置与回归:启动校验、profile 门控、关闭时与升级前一致。"""

    @staticmethod
    def _settings(td, **kw):
        from pathlib import Path
        from deephoto.config import Settings
        return Settings(moonshot_api_key=None, moonshot_base_url="https://example.invalid/v1",
                        chat_model="fake-hitl-cfg", chat_temperature=1, embedding_base_url=None,
                        embedding_api_key=None, embedding_model=None, data_dir=Path(td),
                        max_upload_mb=100, ingestion_version="v1", **kw)

    def test_hitl_requires_persistence_at_startup(self):
        from deephoto.config import validate_qa_context
        with self.assertRaisesRegex(ValueError, "QA_HITL_ENABLED 需要"):
            validate_qa_context(self._settings("/tmp/x", qa_hitl_enabled=True,
                                               qa_persistence_enabled=False))
        validate_qa_context(self._settings("/tmp/x", qa_hitl_enabled=True,
                                           qa_persistence_enabled=True))
        validate_qa_context(self._settings("/tmp/x"))              # 默认关闭不报错

    def test_app_factory_rejects_hitl_without_persistence(self):
        import tempfile
        from deephoto.api.app import create_app
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "QA_HITL_ENABLED"):
                create_app(self._settings(td, qa_hitl_enabled=True, qa_persistence_enabled=False))

    def test_hitl_requires_persistence_via_env(self):
        from deephoto.config import load_settings
        with patch.dict("os.environ", {"DEEPHOTO_QA_HITL_ENABLED": "true",
                                       "DEEPHOTO_QA_PERSISTENCE_ENABLED": "false"}), \
                patch("deephoto.config._load_dotenv"):
            with self.assertRaisesRegex(ValueError, "QA_HITL_ENABLED"):
                load_settings()

    def test_env_defaults_and_bounds(self):
        from deephoto.config import load_settings
        with patch("deephoto.config._load_dotenv"):
            settings = load_settings()
        self.assertFalse(settings.qa_hitl_enabled)
        self.assertEqual(settings.qa_hitl_auto_approve_images, 2)
        self.assertEqual(settings.qa_hitl_timeout_seconds, 600)
        with patch.dict("os.environ", {"DEEPHOTO_QA_HITL_TIMEOUT_SECONDS": "5"}), \
                patch("deephoto.config._load_dotenv"):
            with self.assertRaises(ValueError):
                load_settings()


class HitlProfileTest(_ServiceCase):
    def test_profile_only_changes_when_enabled(self):
        off = self._qa(persistence=True)
        off.settings.qa_hitl_enabled = False
        self.assertNotIn("hitl", off._session_profile())
        on = self._qa()
        self.assertEqual(on._session_profile()["hitl"], {"auto": 0})
        # 开关改变后既有会话拒绝恢复("问答配置已改变")
        on.settings.qa_hitl_enabled = False
        self._model(on, ["普通回答"])
        sid = on.answer(self.conn, self.ctx, "q1", document_id=self.doc_a)["session_id"]
        on.settings.qa_hitl_enabled = True
        self._model(on, ["不应执行"])
        with self.assertRaisesRegex(SessionError, "配置已改变"):
            on.answer(self.conn, self.ctx, "q2", document_id=self.doc_a, session_id=sid)

    def test_build_agent_without_hitl_has_no_interrupt_on(self):
        qa = self._qa()
        qa.settings.qa_hitl_enabled = False
        qa._chat_model = object()
        with patch("deepagents.create_deep_agent") as create:
            qa._build_agent([])
        self.assertNotIn("interrupt_on", create.call_args.kwargs)

    def test_subagent_specs_carry_no_interrupt_on_key(self):
        """F3 的前提:figure_checker/retriever 规格不写 interrupt_on 键(写了会关掉继承)。"""
        from deephoto.agent.subagents import build_subagent_specs
        qa = self._qa()
        tools, _tracker = qa._make_tools(self.ctx)
        for spec in build_subagent_specs(tools):
            self.assertNotIn("interrupt_on", spec)


class HitlRouteTest(unittest.TestCase):
    """§5.6/5.7 的路由契约:409 待审批、/api/qa/approvals 的 400 与 SSE 恢复流。"""

    def _app(self, td, **flags):
        from deephoto.api.app import create_app
        return create_app(HitlConfigTest._settings(
            td, qa_persistence_enabled=True, qa_hitl_enabled=True,
            qa_hitl_auto_approve_images=0, qa_hitl_timeout_seconds=600, **flags))

    def _seed_and_park(self, app):
        """在应用自己的数据目录里造文档/图片,驱动服务到挂起状态;返回 (qa, session_id, model)。"""
        from deephoto.security import LOCAL_CTX
        settings = app.state.settings
        conn = connect(settings.db_path)
        doc = repo.insert_document(conn, tenant_id="default", owner_id="admin", filename="a.pdf",
                                   source_object_key="k", sha256="a" * 64, ingestion_version="v1")
        repo.update_document_status(conn, doc, "ready", page_count=9)
        asset = repo.get_or_create_asset(conn, tenant_id="default", sha256="i" * 64,
                                         object_key="img", width=10, height=10, mime_type="image/png")
        occ = repo.insert_occurrence(conn, tenant_id="default", document_id=doc, ingestion_version="v1",
                                     image_asset_id=asset, page_number=5, bbox=None, figure_number="3.2",
                                     caption="反应路径", extraction_method="embedded_bitmap", needs_review=False)
        conn.commit()
        qa = app.state.qa_service
        qa.knowledge.image_content_blocks = lambda *args: [
            {"type": "text", "text": f"[image:{occ}]"},
            {"type": "image", "base64": "PIXELS", "mime_type": "image/png"}]
        model = ScriptedFakeChatModel(script=[
            {"tool": "inspect_image", "args": {"image_occurrence_id": occ}},
            f"图中箭头向右 [image:{occ}]"],
            model_name=settings.chat_model, ls_provider="openai")
        qa._chat_model = model
        events = list(qa.answer_stream(LOCAL_CTX, "q1", doc))
        assert events[-1]["type"] == "approval_required", events
        return qa, events[-1]["session_id"], model

    @staticmethod
    def _sse_events(response):
        return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]

    def test_409_pending_then_approval_sse_flow(self):
        import tempfile
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as td:
            with TestClient(self._app(td)) as client:
                qa, sid, model = self._seed_and_park(client.app)
                # 待审批时提问 -> 409 + 卡片数据(不得静默丢弃)
                blocked = client.post("/api/qa/stream", json={"question": "q2", "session_id": sid})
                self.assertEqual(blocked.status_code, 409)
                body = blocked.json()
                self.assertEqual(body["code"], "approval_pending")
                self.assertEqual(body["approvals"][0]["approval_id"], "ap_1")
                self.assertIn("图 3.2", body["approvals"][0]["description"])
                self.assertTrue(body["expires_at"])
                # 校验失败 -> 400,图未被触碰
                bad = client.post("/api/qa/approvals", json={
                    "session_id": sid, "decisions": [{"approval_id": "ap_9", "type": "approve"}]})
                self.assertEqual(bad.status_code, 400)
                self.assertEqual(model.calls_made, 1)
                # 批准 -> SSE 恢复流,done 事件带校验后的图片
                ok = client.post("/api/qa/approvals", json={
                    "session_id": sid, "decisions": [{"approval_id": "ap_1", "type": "approve"}]})
                self.assertEqual(ok.status_code, 200)
                events = self._sse_events(ok)
                self.assertEqual(events[0]["type"], "session")
                self.assertEqual(events[-1]["type"], "done")
                self.assertEqual(events[-1]["session_status"], "ready")
                self.assertEqual(len(events[-1]["images"]), 1)
                # 恢复后再提问,会话正常继续
                follow = client.post("/api/qa/stream", json={"question": "q3", "session_id": sid})
                self.assertEqual(follow.status_code, 200)

    def test_approvals_rejected_when_hitl_disabled(self):
        import tempfile
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as td:
            from deephoto.api.app import create_app
            app = create_app(HitlConfigTest._settings(td, qa_persistence_enabled=True))
            with TestClient(app) as client:
                sid = "sess_" + "0" * 32
                result = client.post("/api/qa/approvals", json={
                    "session_id": sid, "decisions": [{"approval_id": "ap_1", "type": "approve"}]})
                self.assertEqual(result.status_code, 400)
                self.assertIn("人工审批未启用", result.json()["detail"])


if __name__ == "__main__":
    unittest.main()
