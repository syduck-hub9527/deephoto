"""错误信息脱敏:密钥、Authorization、data URL 不得进入库或日志摘要。"""

import unittest

import _bootstrap  # noqa: F401

from deephoto.sanitize import error_summary, redact_secrets


class _HttpError(Exception):
    status_code = 401


class SanitizeTest(unittest.TestCase):
    def test_configured_secret_value_is_removed(self):
        text = redact_secrets("upstream said key=MY-DESC-KEY-12345 is bad", ["MY-DESC-KEY-12345"])
        self.assertNotIn("MY-DESC-KEY-12345", text)

    def test_bearer_and_sk_patterns(self):
        text = redact_secrets("Authorization: Bearer abc.def-123 ; Incorrect API key: sk-abc123****wxyz")
        self.assertNotIn("abc.def-123", text)
        self.assertNotIn("sk-abc123", text)

    def test_data_url_and_long_blob_removed(self):
        text = redact_secrets("bad image data:image/png;base64,AAAABBBBCCCC== tail " + "A" * 300)
        self.assertNotIn("AAAABBBB", text)
        self.assertIn("tail", text)

    def test_summary_keeps_class_status_and_truncates(self):
        summary = error_summary(_HttpError("x " * 400), max_chars=50)
        self.assertTrue(summary.startswith("_HttpError(status=401): "))
        self.assertLessEqual(len(summary), len("_HttpError(status=401): ") + 51)
        self.assertTrue(summary.endswith("…"))

    def test_summary_without_message_or_status(self):
        self.assertEqual(error_summary(KeyError()), "KeyError")

    def test_short_secret_values_are_ignored(self):
        # 过短的"密钥"(如空串、'k')不做精确替换,避免把正常文字抹成一团
        self.assertEqual(redact_secrets("a key here", ["", None, "k"]), "a key here")


if __name__ == "__main__":
    unittest.main()
