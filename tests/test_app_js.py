"""Run the offline frontend transport/state contracts without a browser."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), '需要 node')
class AppJsTest(unittest.TestCase):
    def test_app_js(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('js_app_test.cjs'))],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
