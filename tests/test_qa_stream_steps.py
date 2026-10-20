"""用真实编译图验证流式过程说明、最终正文、断流和 HITL 恢复。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel
from deephoto import repo
from deephoto.agent.knowledge import KnowledgeService
from deephoto.agent.qa import QAService
from deephoto.db import connect, init_db
from deephoto.security import LOCAL_CTX


class StreamStepsTest(unittest.TestCase):
    def setUp(self):
        # 与现有 API 测试一样，Windows 不让线程池缓存的连接阻断临时目录清理。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / 'test.db'
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.ctx = LOCAL_CTX
        self.doc = repo.insert_document(self.conn, tenant_id=self.ctx.tenant_id,
            owner_id=self.ctx.user_id, filename='fixture.pdf', source_object_key='fixture',
            sha256='d' * 64, ingestion_version='v1')
        repo.update_document_status(self.conn, self.doc, 'ready', page_count=1)
        self.chunks = [repo.insert_chunk(self.conn, tenant_id=self.ctx.tenant_id,
            document_id=self.doc, ingestion_version='v1', section='测试', text=f'测试正文 {number}',
            page_start=1, page_end=1, paragraph_ids=[], referenced_image_ids=[], nearby_image_ids=[])
            for number in (1, 2)]
        asset = repo.get_or_create_asset(self.conn, tenant_id=self.ctx.tenant_id,
            sha256='i' * 64, object_key='image', width=1, height=1, mime_type='image/png')
        self.occ = repo.insert_occurrence(self.conn, tenant_id=self.ctx.tenant_id,
            document_id=self.doc, ingestion_version='v1', image_asset_id=asset, page_number=1,
            bbox=None, figure_number='1', caption='测试图', extraction_method='embedded_bitmap', needs_review=False)
        self.conn.commit()
        self.services = []
        self.executions = []

    def tearDown(self):
        for qa in self.services:
            qa.close()
        self.conn.close()
        self.tmp.cleanup()

    def service(self, script, *, persistence=True, hitl=False, model_type=ScriptedFakeChatModel):
        knowledge = KnowledgeService(None, None)

        def search(*args):
            self.executions.append('search')
            return {'chunks': [{'chunk_id': chunk, 'text': '测试正文'} for chunk in self.chunks], 'images': []}

        knowledge.search = search
        knowledge.image_content_blocks = lambda *args: [{'type': 'text', 'text': '已查看原图'}]
        settings = SimpleNamespace(db_path=self.db_path, qa_persistence_enabled=persistence,
            qa_middleware_enabled=True, qa_subagents_enabled=False, qa_kb_vfs_enabled=False,
            qa_skills_enabled=False, qa_hitl_enabled=hitl, qa_hitl_auto_approve_images=0,
            qa_hitl_timeout_seconds=600, qa_main_max_model_calls=6, qa_main_recursion_limit=40,
            chat_model='fake-stream-preamble')
        qa = QAService(settings, knowledge)
        qa._chat_model = model_type(script=script, model_name=settings.chat_model, ls_provider='openai')
        self.services.append(qa)
        return qa

    def script(self, preamble='先补充检索原理部分。', final=None):
        return [{'text': preamble, 'tool': 'search_knowledge', 'args': {'query': '测试'}},
                final or f'最终回答 [chunk:{self.chunks[1]}]']

    def test_mixed_tool_text_excluded_with_and_without_persistence(self):
        for persistence in (False, True):
            with self.subTest(persistence=persistence):
                qa = self.service(self.script(), persistence=persistence)
                self.executions.clear()
                events = []
                for event in qa.answer_stream(self.ctx, '测试', self.doc):
                    self.executions.append(event['type'])
                    events.append(event)
                steps = [e for e in events if e['type'] == 'step']
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0]['text'], '先补充检索原理部分。')
                step_index = events.index(steps[0])
                self.assertEqual(events[step_index - 1]['type'], 'token')
                self.assertEqual(events[step_index - 1]['mid'], steps[0]['mid'])
                self.assertEqual(''.join(e['text'] for e in events[:step_index] if e['type'] == 'token'), steps[0]['text'])
                self.assertNotIn(steps[0]['mid'], [e['mid'] for e in events[step_index + 1:] if e['type'] == 'token'])
                self.assertEqual(events[-1]['type'], 'done')
                self.assertEqual(events[-1]['answer'], f'最终回答 [chunk:{self.chunks[1]}]')
                self.assertLess(self.executions.index('step'), self.executions.index('search'))

    def test_stream_sync_and_checkpoint_final_answers_match(self):
        for persistence in (False, True):
            with self.subTest(persistence=persistence):
                streamed = self.service(self.script(), persistence=persistence)
                done = list(streamed.answer_stream(self.ctx, '测试', self.doc))[-1]
                sync = self.service(self.script(), persistence=persistence).answer(self.conn, self.ctx, '测试', self.doc)
                self.assertEqual(done['answer'], sync['answer'])
                self.assertEqual(done['citations'], sync['citations'])
                if persistence:
                    history = streamed.session_history(self.conn, self.ctx, done['session_id'])
                    self.assertEqual(history['messages'][-1]['answer'], done['answer'])
                    self.assertEqual(history['messages'][-1]['citations'], done['citations'])

    def test_two_tool_rounds_have_two_steps_and_only_final_citations(self):
        first = f'先看绪论 [chunk:{self.chunks[0]}]。'
        second = '再核对参数。'
        script = [self.script(first)[0], self.script(second)[0], f'最终回答 [chunk:{self.chunks[1]}]']
        events = list(self.service(script).answer_stream(self.ctx, '测试', self.doc))
        steps = [e for e in events if e['type'] == 'step']
        self.assertEqual([e['text'] for e in steps], [first, second])
        self.assertEqual(len({e['mid'] for e in steps}), 2)
        self.assertEqual([c['chunk_id'] for c in events[-1]['citations']], [self.chunks[1]])

    def test_interrupted_final_call_uses_only_its_partial_text(self):
        class InterruptedFinal(ScriptedFakeChatModel):
            def _stream(self, *args, **kwargs):
                for chunk in super()._stream(*args, **kwargs):
                    yield chunk
                    if self.calls_made == 2 and chunk.message.content:
                        raise RuntimeError('FINAL_STREAM_INTERRUPTED')

        for persistence in (False, True):
            with self.subTest(persistence=persistence):
                qa = self.service(self.script(final='最终回答的一部分，后面尚未传输。'),
                                  persistence=persistence, model_type=InterruptedFinal)
                events = list(qa.answer_stream(self.ctx, '测试', self.doc))
                final_mid = [e['mid'] for e in events if e['type'] == 'token'][-1]
                partial = ''.join(e['text'] for e in events if e['type'] == 'token' and e['mid'] == final_mid)
                self.assertTrue(partial)
                self.assertEqual(events[-1]['type'], 'done')
                self.assertEqual(events[-1]['answer'], partial)
                self.assertNotIn('先补充', partial)
                self.assertTrue(any(e['type'] == 'warning' for e in events))
                if persistence:
                    self.assertEqual(events[-1]['session_status'], 'failed')

    def test_interruption_after_only_tool_call_does_not_return_preamble(self):
        class InterruptedTool(ScriptedFakeChatModel):
            def _stream(self, *args, **kwargs):
                for chunk in super()._stream(*args, **kwargs):
                    yield chunk
                    if chunk.message.tool_call_chunks:
                        raise RuntimeError('TOOL_STREAM_INTERRUPTED')

        events = list(self.service(self.script(), model_type=InterruptedTool).answer_stream(self.ctx, '测试', self.doc))
        self.assertEqual(events[-1]['type'], 'error')
        self.assertIn('模型未产生回答', events[-1]['detail'])
        self.assertFalse(any(e['type'] == 'done' for e in events))

    def test_plain_answer_has_no_step(self):
        events = list(self.service(['普通回答']).answer_stream(self.ctx, '测试', self.doc))
        self.assertFalse(any(e['type'] == 'step' for e in events))
        self.assertEqual(events[-1]['answer'], '普通回答')

    def test_empty_final_answer_does_not_reuse_tool_preamble(self):
        for persistence in (False, True):
            with self.subTest(persistence=persistence):
                events = list(self.service([self.script()[0], ''], persistence=persistence)
                              .answer_stream(self.ctx, '测试', self.doc))
                self.assertEqual(events[-1]['type'], 'error')
                self.assertIn('模型未产生回答', events[-1]['detail'])
                self.assertFalse(any(e['type'] == 'done' for e in events))

    def test_missing_values_falls_back_to_last_non_tool_call(self):
        qa = self.service(self.script(), persistence=False)
        build = qa._build_agent

        def without_values(*args, **kwargs):
            agent = build(*args, **kwargs)

            def stream(*stream_args, **stream_kwargs):
                for mode, data in agent.stream(*stream_args, **stream_kwargs):
                    if mode != 'values':
                        yield mode, data

            return SimpleNamespace(stream=stream)

        qa._build_agent = without_values
        events = list(qa.answer_stream(self.ctx, '测试', self.doc))
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[-1]['answer'], f'最终回答 [chunk:{self.chunks[1]}]')

    def test_final_state_with_only_tool_call_is_not_an_answer(self):
        qa = self.service(self.script(), persistence=False)
        build = qa._build_agent

        def pause_after_model(*args, **kwargs):
            agent = build(*args, **kwargs)

            def stream(*stream_args, **stream_kwargs):
                yield from agent.stream(*stream_args, interrupt_after=['model'], **stream_kwargs)

            return SimpleNamespace(stream=stream)

        qa._build_agent = pause_after_model
        events = list(qa.answer_stream(self.ctx, '测试', self.doc))
        self.assertEqual(events[-1]['type'], 'error')
        self.assertIn('模型未产生回答', events[-1]['detail'])
        self.assertFalse(any(e['type'] == 'done' for e in events))

    def test_hitl_step_precedes_pause_and_resume_has_clean_answer(self):
        preamble = '先查看图中标签。'
        script = [{'text': preamble, 'tool': 'inspect_image', 'args': {'image_occurrence_id': self.occ}},
                  f'最终回答\n\n[image:{self.occ}]']
        qa = self.service(script, hitl=True)
        events = list(qa.answer_stream(self.ctx, '测试', self.doc))
        self.assertEqual(events[-1]['type'], 'approval_required')
        self.assertEqual([e['text'] for e in events if e['type'] == 'step'], [preamble])
        self.assertFalse(any(e['type'] == 'done' for e in events))
        pending = events[-1]
        resumed = list(qa.resolve_approvals(self.ctx, pending['session_id'],
            [{'approval_id': a['approval_id'], 'type': 'approve'} for a in pending['approvals']]))
        self.assertEqual(resumed[-1]['type'], 'done')
        self.assertEqual(resumed[-1]['session_id'], pending['session_id'])
        self.assertNotIn(preamble, resumed[-1]['answer'])
        self.assertEqual(resumed[-1]['answer'], f'最终回答\n\n[image:{self.occ}]')
