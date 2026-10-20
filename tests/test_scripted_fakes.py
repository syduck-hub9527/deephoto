"""ScriptedFakeChatModel 的混合文字 / 工具脚本与既有脚本兼容性。"""
import unittest

import _bootstrap  # noqa: F401
from support.fakes import ScriptedFakeChatModel


class ScriptedFakeTest(unittest.TestCase):
    def test_mixed_text_and_tools_invoke(self):
        for item in ({'tool': 'probe', 'args': {'value': 1}},
                     {'tools': [{'name': 'probe', 'args': {'value': 1}}]}):
            with self.subTest(item=item):
                message = ScriptedFakeChatModel(script=[{'text': '先检索。', **item}]).invoke('q')
                self.assertEqual(message.content, '先检索。')
                self.assertEqual(message.tool_calls[0]['name'], 'probe')
                self.assertEqual(message.tool_calls[0]['args'], {'value': 1})

    def test_mixed_stream_text_precedes_all_tool_chunks(self):
        preamble = '先补充检索原理与仪器部分，再回答用户问题。'
        for item in ({'tool': 'probe'}, {'tools': [{'name': 'probe'}, {'name': 'other'}]}):
            with self.subTest(item=item):
                chunks = list(ScriptedFakeChatModel(script=[{'text': preamble, **item}]).stream('q'))
                first_tool = next(i for i, c in enumerate(chunks) if c.tool_call_chunks)
                self.assertEqual(''.join(c.content for c in chunks[:first_tool]), preamble)
                self.assertTrue(all(not c.content for c in chunks[first_tool:]))
                self.assertGreater(first_tool, 1)  # 按 STREAM_CHUNK_SIZE 分多块。

    def test_old_tool_and_plain_text_scripts_unchanged(self):
        chunks = list(ScriptedFakeChatModel(script=[{'tool': 'probe'}]).stream('q'))
        self.assertTrue(any(c.tool_call_chunks for c in chunks))
        self.assertEqual(''.join(c.content for c in chunks), '')
        message = ScriptedFakeChatModel(script=[{'tool': 'probe'}]).invoke('q')
        self.assertEqual(message.content, '')
        plain = list(ScriptedFakeChatModel(script=['原来的普通回答']).stream('q'))
        self.assertEqual(''.join(c.content for c in plain), '原来的普通回答')
        self.assertFalse(any(c.tool_call_chunks for c in plain))
