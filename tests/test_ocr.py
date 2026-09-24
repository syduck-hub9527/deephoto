import json
import unittest

import _bootstrap  # noqa: F401
from deephoto.ocr import (
    DEFAULT_LOCAL_OCR_URL,
    OCRConfig,
    OCRConfigurationError,
    OpenAICompatibleOCRProvider,
)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload


class OCRConfigTest(unittest.TestCase):
    def test_local_uses_safe_default_endpoint(self):
        config = OCRConfig(provider="local", model="my-ocr").normalized()
        self.assertEqual(config.base_url, DEFAULT_LOCAL_OCR_URL)

    def test_third_party_requires_model_and_url(self):
        with self.assertRaises(OCRConfigurationError):
            OCRConfig(provider="third_party", model="my-ocr").normalized()

    def test_remote_alias_is_supported(self):
        config = OCRConfig(
            provider="openai_compatible", model="my-ocr", base_url="https://ocr.example/v1",
        ).normalized()
        self.assertEqual(config.provider, "third_party")


class OCRProviderTest(unittest.TestCase):
    def test_posts_image_and_reads_openai_content_blocks(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return _Response({
                "choices": [{"message": {"content": [
                    {"type": "text", "text": "第一行\n"},
                    {"type": "text", "text": "第二行"},
                ]}}],
            })

        provider = OpenAICompatibleOCRProvider(
            OCRConfig(provider="local", model="ocr", base_url="http://localhost:9000/v1",
                      api_key="secret", timeout_seconds=12),
            opener=opener,
        )
        result = provider.recognize(b"png", "image/png")

        self.assertEqual(result.text, "第一行\n第二行")
        self.assertEqual(requests[0][1], 12)
        self.assertEqual(requests[0][0].get_header("Authorization"), "Bearer secret")
        payload = json.loads(requests[0][0].data.decode("utf-8"))
        image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertTrue(requests[0][0].full_url.endswith("/v1/chat/completions"))


if __name__ == "__main__":
    unittest.main()
