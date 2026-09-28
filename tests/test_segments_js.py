"""segments.js(前端正文分段/内嵌图片渲染)的 node 测试入口。

node 不可用时跳过;断言细节在 tests/js_segments_test.cjs。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_NODE_SCRIPT = Path(__file__).parent / "js_segments_test.cjs"


@unittest.skipUnless(shutil.which("node"), "需要 node 运行前端纯函数测试")
class SegmentsJsTest(unittest.TestCase):
    def test_segments_js(self):
        result = subprocess.run(["node", str(_NODE_SCRIPT)], capture_output=True, text=True)
        if result.returncode != 0:
            self.fail(f"node 断言失败:\n{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
