"""图片描述模型独立配置(DESCRIPTION_*)测试:env 解析、启动校验、工厂、解析硬化。

模拟请求,不调用真实付费 API;langchain_openai/pydantic 用假模块注入。
"""

import os
import sys
import types
import unittest

import _bootstrap  # noqa: F401

from deephoto.config import Settings, load_settings
from deephoto.pipeline.describe import _response_text, parse_description_json

_DESC_VARS = [
    "DEEPHOTO_DESCRIPTION_ENABLED", "DEEPHOTO_DESCRIPTION_API_KEY",
    "DEEPHOTO_DESCRIPTION_BASE_URL", "DEEPHOTO_DESCRIPTION_MODEL",
    "DEEPHOTO_DESCRIPTION_REASONING_EFFORT", "DEEPHOTO_DESCRIPTION_TIMEOUT_SECONDS",
    "DEEPHOTO_DESCRIPTION_MAX_RETRIES", "DEEPHOTO_DESCRIPTION_MAX_TOKENS",
]


def _base_settings(**overrides) -> Settings:
    kwargs = dict(
        moonshot_api_key=None, moonshot_base_url="", chat_model="k3", chat_temperature=1.0,
        embedding_base_url=None, embedding_api_key=None, embedding_model=None,
        data_dir="/tmp/x", max_upload_mb=100, ingestion_version="v2", mineru_api_key=None,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class EnvTestBase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _DESC_VARS}
        for key in _DESC_VARS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class OldConstructionTest(unittest.TestCase):
    def test_old_settings_construction_still_works(self):
        s = _base_settings()                       # 旧构造方式(无 description_* 参数)
        self.assertFalse(s.description_enabled)
        self.assertEqual(s.description_model, "qwen3.8-omni-flash")
        self.assertEqual((s.description_timeout_seconds, s.description_max_retries,
                          s.description_max_tokens), (90.0, 1, 1024))


class EnvParsingTest(EnvTestBase):
    def test_values_and_types(self):
        os.environ.update({
            "DEEPHOTO_DESCRIPTION_ENABLED": "1",
            "DEEPHOTO_DESCRIPTION_API_KEY": "k",
            "DEEPHOTO_DESCRIPTION_BASE_URL": "https://example.cn/v1",
            "DEEPHOTO_DESCRIPTION_MODEL": "m-x",
            "DEEPHOTO_DESCRIPTION_REASONING_EFFORT": "",
            "DEEPHOTO_DESCRIPTION_TIMEOUT_SECONDS": "45.5",
            "DEEPHOTO_DESCRIPTION_MAX_RETRIES": "0",
            "DEEPHOTO_DESCRIPTION_MAX_TOKENS": "512",
        })
        s = load_settings()
        self.assertTrue(s.description_enabled)
        self.assertEqual((s.description_api_key, s.description_base_url, s.description_model),
                         ("k", "https://example.cn/v1", "m-x"))
        self.assertIsNone(s.description_reasoning_effort)   # 空字符串 -> 不发送该参数
        self.assertEqual((s.description_timeout_seconds, s.description_max_retries,
                          s.description_max_tokens), (45.5, 0, 512))

    def test_bool_variants(self):
        os.environ.update({   # true 分支需通过启动校验
            "DEEPHOTO_DESCRIPTION_API_KEY": "k",
            "DEEPHOTO_DESCRIPTION_BASE_URL": "https://example.cn/v1",
        })
        for raw, expected in (("true", True), ("false", False), ("1", True), ("0", False),
                              ("yes", True), ("no", False)):
            os.environ["DEEPHOTO_DESCRIPTION_ENABLED"] = raw
            self.assertIs(load_settings().description_enabled, expected, raw)

    def test_bool_invalid_names_variable(self):
        os.environ["DEEPHOTO_DESCRIPTION_ENABLED"] = "maybe"
        with self.assertRaisesRegex(ValueError, "DEEPHOTO_DESCRIPTION_ENABLED"):
            load_settings()

    def test_number_invalid_even_when_disabled(self):
        os.environ["DEEPHOTO_DESCRIPTION_MAX_RETRIES"] = "abc"   # 关闭时数字仍应合法
        with self.assertRaisesRegex(ValueError, "DEEPHOTO_DESCRIPTION_MAX_RETRIES"):
            load_settings()


class ValidationTest(EnvTestBase):
    def _enabled(self, **extra):
        os.environ.update({
            "DEEPHOTO_DESCRIPTION_ENABLED": "true",
            "DEEPHOTO_DESCRIPTION_API_KEY": "secret-should-not-leak",
            "DEEPHOTO_DESCRIPTION_BASE_URL": "https://example.cn/v1",
        })
        os.environ.update(extra)

    def test_missing_key_and_url_names_variable_without_value(self):
        os.environ["DEEPHOTO_DESCRIPTION_ENABLED"] = "true"
        with self.assertRaises(ValueError) as ctx:
            load_settings()
        msg = str(ctx.exception)
        self.assertIn("DEEPHOTO_DESCRIPTION_API_KEY", msg)
        self.assertIn("DEEPHOTO_DESCRIPTION_BASE_URL", msg)
        self.assertNotIn("secret", msg.lower())

    def test_bad_urls_rejected(self):
        for bad in ("not-a-url", "https://user:pass@example.cn/v1",
                    "https://example.cn/v1?x=1", "https://example.cn/v1/chat/completions",
                    "https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                    "https://YOUR_WORKSPACE_ID.example.cn/v1"):
            self._enabled(DEEPHOTO_DESCRIPTION_BASE_URL=bad)
            with self.assertRaisesRegex(ValueError, "DEEPHOTO_DESCRIPTION_BASE_URL", msg=bad):
                load_settings()

    def test_disabled_does_not_require_key(self):
        os.environ["DEEPHOTO_DESCRIPTION_ENABLED"] = "false"   # 无 key/URL 也可启动
        self.assertFalse(load_settings().description_enabled)

    def test_enabled_ok(self):
        self._enabled()
        self.assertTrue(load_settings().description_enabled)


class FactoryTest(unittest.TestCase):
    def test_disabled_returns_none_without_langchain(self):
        from deephoto.llm import build_description_model
        # 关闭时直接返回 None,不触发 langchain 导入(本环境未安装)
        self.assertIsNone(build_description_model(_base_settings()))

    def test_enabled_uses_independent_config(self):
        captured = {}
        fake_openai = types.ModuleType("langchain_openai")
        fake_openai.ChatOpenAI = lambda **kw: captured.update(kw) or object()
        fake_pydantic = types.ModuleType("pydantic")
        fake_pydantic.SecretStr = lambda v: f"secret({v})"
        saved = {k: sys.modules.get(k) for k in ("langchain_openai", "pydantic")}
        sys.modules["langchain_openai"] = fake_openai
        sys.modules["pydantic"] = fake_pydantic
        try:
            from deephoto.llm import build_description_model
            settings = _base_settings(
                description_enabled=True, description_api_key="desc-key",
                description_base_url="https://desc.example.cn/v1",
                description_model="m-desc", description_reasoning_effort="none",
                description_timeout_seconds=45.0, description_max_retries=3,
                description_max_tokens=256)
            build_description_model(settings)
        finally:
            for key, value in saved.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value
        self.assertEqual(captured["model"], "m-desc")
        self.assertEqual(captured["api_key"], "secret(desc-key)")      # 不回退聊天密钥
        self.assertEqual(captured["base_url"], "https://desc.example.cn/v1")
        self.assertEqual((captured["timeout"], captured["max_retries"]), (45.0, 3))
        # max_tokens 走 extra_body(避免被 langchain-openai 改写成 max_completion_tokens)
        self.assertEqual(captured["extra_body"], {"max_tokens": 256})
        self.assertNotIn("max_tokens", captured)
        self.assertEqual(captured["reasoning_effort"], "none")         # 顶层关思考
        self.assertNotIn("temperature", captured)                      # 不继承聊天参数
        self.assertNotIn("enable_thinking", captured)                  # 不混用其他型号参数

    def test_empty_effort_omits_param(self):
        captured = {}
        fake_openai = types.ModuleType("langchain_openai")
        fake_openai.ChatOpenAI = lambda **kw: captured.update(kw) or object()
        fake_pydantic = types.ModuleType("pydantic")
        fake_pydantic.SecretStr = lambda v: v
        saved = {k: sys.modules.get(k) for k in ("langchain_openai", "pydantic")}
        sys.modules["langchain_openai"] = fake_openai
        sys.modules["pydantic"] = fake_pydantic
        try:
            from deephoto.llm import build_description_model
            build_description_model(_base_settings(
                description_enabled=True, description_api_key="k",
                description_base_url="https://x.cn/v1", description_reasoning_effort=None))
        finally:
            for key, value in saved.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value
        self.assertNotIn("reasoning_effort", captured)                 # 空 -> 不发送该参数


class PublicValidationTest(unittest.TestCase):
    """create_app(settings) 直传 Settings 时不能绕过启动校验。"""

    def test_validate_description_rejects_enabled_without_key(self):
        from deephoto.config import validate_description
        with self.assertRaisesRegex(ValueError, "DEEPHOTO_DESCRIPTION_API_KEY"):
            validate_description(_base_settings(
                description_enabled=True, description_base_url="https://x.cn/v1"))

    def test_validate_description_noop_when_disabled(self):
        from deephoto.config import validate_description
        validate_description(_base_settings())   # 不抛

    def test_create_app_validates_directly_passed_settings(self):
        try:
            from deephoto.api.app import create_app
        except ImportError:
            self.skipTest("fastapi 未安装")
        with self.assertRaisesRegex(ValueError, "DEEPHOTO_DESCRIPTION_BASE_URL"):
            create_app(_base_settings(description_enabled=True, description_api_key="k"))

    def test_plain_http_detection(self):
        from deephoto.config import description_uses_plain_http as plain
        on = dict(description_enabled=True, description_api_key="k")
        self.assertTrue(plain(_base_settings(description_base_url="http://api.example.cn/v1", **on)))
        self.assertFalse(plain(_base_settings(description_base_url="https://api.example.cn/v1", **on)))
        self.assertFalse(plain(_base_settings(description_base_url="http://localhost:8000/v1", **on)))
        self.assertFalse(plain(_base_settings(description_base_url="http://127.0.0.1:8000/v1", **on)))
        self.assertFalse(plain(_base_settings()))                # 关闭


class ParseHardeningTest(unittest.TestCase):
    def test_valid_ok(self):
        result = parse_description_json('{"visible_summary": "图", "visible_labels": ["a"]}')
        self.assertEqual(result["diagnostic"]["result"], "ok")

    def test_non_object_root_is_parse_failed(self):
        self.assertEqual(parse_description_json('[1, 2]')["diagnostic"]["result"], "parse_failed")
        self.assertEqual(parse_description_json('not json')["diagnostic"]["result"], "parse_failed")

    def test_truncated_is_parse_failed(self):
        self.assertEqual(parse_description_json('{"visible_summary": "半段')["diagnostic"]["result"],
                         "parse_failed")

    def test_wrong_field_types_are_parse_failed(self):
        self.assertEqual(parse_description_json(
            '{"visible_labels": "abc"}')["diagnostic"]["result"], "parse_failed")   # 字符串被逐字符拆开前拦截
        self.assertEqual(parse_description_json(
            '{"visible_summary": ["x"]}')["diagnostic"]["result"], "parse_failed")
        self.assertEqual(parse_description_json(
            '{"uncertain_details": "x"}')["diagnostic"]["result"], "parse_failed")

    def test_response_text_extracts_only_text_blocks(self):
        blocks = [{"type": "text", "text": '{"a": 1}'},
                  {"type": "image_url", "image_url": {"url": "data:..."}},
                  {"type": "text", "text": " 后缀"}]
        self.assertEqual(_response_text(blocks), '{"a": 1} 后缀')
        self.assertEqual(_response_text("plain"), "plain")
        self.assertEqual(_response_text([]), "")


if __name__ == "__main__":
    unittest.main()
