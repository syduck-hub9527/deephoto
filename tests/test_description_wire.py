"""图片描述模型的"线上请求"测试:真实 ChatOpenAI + httpx.MockTransport,不联网、不花钱。

验证假模块测不到的四件事:请求体字段、流式聚合、中断不算成功、聊天/描述配置互不串用。
依赖 langchain-openai、openai、httpx;未安装时整体跳过。
"""

import json
import unittest

import _bootstrap  # noqa: F401

try:
    import httpx
    import openai
    from langchain_core.messages import HumanMessage
    HAVE_STACK = True
except ImportError:               # pragma: no cover
    HAVE_STACK = False

from deephoto.config import Settings
from deephoto.llm import build_chat_model, build_description_model
from deephoto.pipeline.describe import describe_image

DESC_URL = "https://desc.example.cn/compatible-mode/v1"
CHAT_URL = "https://chat.example.cn/coding/v1"


def _settings(**overrides) -> Settings:
    kwargs = dict(
        moonshot_api_key="chat-key", moonshot_base_url=CHAT_URL, chat_model="k3", chat_temperature=1.0,
        embedding_base_url=None, embedding_api_key=None, embedding_model=None,
        data_dir="/tmp/x", max_upload_mb=100, ingestion_version="v2", mineru_api_key=None,
        description_enabled=True, description_api_key="desc-key", description_base_url=DESC_URL,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _chunk(delta, finish=None):
    return "data: " + json.dumps({
        "id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


def _sse(*parts: str) -> bytes:
    return "".join(parts).encode()


def _attach(model, base_url, api_key, handler):
    """把真实 ChatOpenAI 的 HTTP 层换成 MockTransport(其余序列化逻辑全部真实)。"""
    client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    model.root_client = client
    model.client = client.chat.completions
    return model


def _sse_handler(captured, body: bytes):
    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    return handler


def _describe(model):
    return describe_image(model, b"\x89PNG", "image/png", "图1.1", "第1章", "正文")


@unittest.skipUnless(HAVE_STACK, "需要 langchain-openai / openai / httpx")
class DescriptionWireTest(unittest.TestCase):
    def _model(self, handler, **overrides):
        s = _settings(**overrides)
        return _attach(build_description_model(s), DESC_URL, "desc-key", handler)

    def test_request_body_and_headers(self):
        cap = {}
        good = _sse(_chunk({"content": '{"visible_summary": "图"}'}, "stop"), "data: [DONE]\n\n")
        model = self._model(_sse_handler(cap, good))
        result = _describe(model)
        self.assertEqual(result["diagnostic"]["result"], "ok")
        body = cap["body"]
        self.assertTrue(cap["url"].startswith(DESC_URL))
        self.assertEqual(cap["auth"], "Bearer desc-key")                # 描述密钥,不是聊天密钥
        self.assertEqual(body["model"], "qwen3.8-omni-flash")
        self.assertEqual(body["reasoning_effort"], "none")              # 顶层关思考
        self.assertIs(body["stream"], True)
        self.assertEqual(body["max_tokens"], 1024)                      # 经典字段
        self.assertNotIn("max_completion_tokens", body)                 # 不被改写
        for banned in ("enable_thinking", "thinking_budget", "temperature", "tools"):
            self.assertNotIn(banned, body)
        parts = body["messages"][0]["content"]
        image = next(p for p in parts if p["type"] == "image_url")
        self.assertTrue(image["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_empty_effort_is_not_sent(self):
        cap = {}
        good = _sse(_chunk({"content": "{}"}, "stop"), "data: [DONE]\n\n")
        model = self._model(_sse_handler(cap, good), description_reasoning_effort=None)
        _describe(model)
        self.assertNotIn("reasoning_effort", cap["body"])

    def test_stream_is_aggregated_and_reasoning_ignored(self):
        cap = {}
        usage = "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": 1,
                                       "model": "m", "choices": [],
                                       "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                                                 "total_tokens": 3}}) + "\n\n"
        body = _sse(
            _chunk({"role": "assistant", "content": ""}),
            _chunk({"reasoning_content": "这段思考不能进结果"}),
            _chunk({"content": '{"visible_summary": "分'}),
            _chunk({"content": '片", "visible_labels": ["a", "b"]}'}),
            _chunk({}, "stop"), usage, "data: [DONE]\n\n")
        result = _describe(self._model(_sse_handler(cap, body)))
        self.assertEqual(result["diagnostic"]["result"], "ok")
        self.assertEqual(result["visible_summary"], "分片")
        self.assertEqual(result["visible_labels"], ["a", "b"])
        self.assertNotIn("思考", json.dumps(result, ensure_ascii=False))

    def test_truncated_stream_is_not_success(self):
        cap = {}
        body = _sse(_chunk({"content": '{"visible_summary": "半'}))          # 无 finish/[DONE]
        result = _describe(self._model(_sse_handler(cap, body)))
        self.assertEqual(result["diagnostic"]["result"], "parse_failed")

    def test_length_finish_is_not_success(self):
        cap = {}
        body = _sse(_chunk({"content": '{"visible_summary": "很长'}), _chunk({}, "length"),
                    "data: [DONE]\n\n")
        result = _describe(self._model(_sse_handler(cap, body)))
        self.assertEqual(result["diagnostic"]["result"], "parse_failed")

    def test_network_error_mid_stream_raises(self):
        def handler(request):
            def gen():
                yield _chunk({"content": '{"visible_summary": "半'}).encode()
                raise httpx.ReadError("boom")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())
        # 关键是"抛异常、不当成功"。异常类型随版本不同:新版被 openai 包装为
        # APIConnectionError,langchain-openai 0.3.x 上是原始 httpx.HTTPError(ReadError)
        with self.assertRaises((openai.APIConnectionError, httpx.HTTPError)):
            _describe(self._model(handler))


@unittest.skipUnless(HAVE_STACK, "需要 langchain-openai / openai / httpx")
class ChatVsDescriptionIsolationTest(unittest.TestCase):
    """聊天与描述同时存在时,各发往各的端点、用各的密钥;问答请求体不受描述参数污染。"""

    def test_each_uses_its_own_endpoint_key_and_params(self):
        desc_cap, chat_cap = {}, {}
        desc_body = _sse(_chunk({"content": "{}"}, "stop"), "data: [DONE]\n\n")

        def chat_handler(request):
            chat_cap["url"] = str(request.url)
            chat_cap["auth"] = request.headers.get("authorization")
            chat_cap["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "created": 1, "model": "k3",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "答"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        s = _settings()
        desc = _attach(build_description_model(s), DESC_URL, "desc-key", _sse_handler(desc_cap, desc_body))
        chat = _attach(build_chat_model(s), CHAT_URL, "chat-key", chat_handler)
        _describe(desc)
        chat.invoke([HumanMessage(content="hi")])

        self.assertTrue(desc_cap["url"].startswith(DESC_URL))
        self.assertTrue(chat_cap["url"].startswith(CHAT_URL))
        self.assertEqual(desc_cap["auth"], "Bearer desc-key")
        self.assertEqual(chat_cap["auth"], "Bearer chat-key")
        self.assertEqual(chat_cap["body"]["model"], "k3")
        self.assertEqual(chat_cap["body"]["temperature"], 1.0)
        for leaked in ("reasoning_effort", "max_tokens", "max_completion_tokens"):
            self.assertNotIn(leaked, chat_cap["body"])                   # 问答请求未受影响

    def test_description_works_without_chat_key(self):
        s = _settings(moonshot_api_key=None)
        cap = {}
        body = _sse(_chunk({"content": '{"visible_summary": "图"}'}, "stop"), "data: [DONE]\n\n")
        model = _attach(build_description_model(s), DESC_URL, "desc-key", _sse_handler(cap, body))
        self.assertEqual(_describe(model)["diagnostic"]["result"], "ok")  # 描述不依赖聊天密钥
        with self.assertRaises(RuntimeError):                            # 问答仍要求聊天密钥
            build_chat_model(s)


if __name__ == "__main__":
    unittest.main()
