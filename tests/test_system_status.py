"""Public system projection must follow runtime state and never leak credentials."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from fastapi.testclient import TestClient
from test_upload_route import _settings
from deephoto.api.app import create_app


class SystemStatusTest(unittest.TestCase):
    def check_status(self, hybrid, enabled):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            settings = _settings(Path(root), moonshot_api_key="SECRET-CHAT",
                                 moonshot_base_url="https://private-chat.invalid/v1",
                                 embedding_model="test-embed", qa_persistence_enabled=True,
                                 qa_hitl_enabled=enabled, qa_hitl_auto_approve_images=3,
                                 qa_subagents_enabled=enabled, qa_kb_vfs_enabled=enabled,
                                 qa_skills_enabled=enabled)
            index = SimpleNamespace(embeddings=object() if hybrid else None,
                                    embedding_version="test-embed#1024" if hybrid else None)
            with patch('deephoto.api.app.build_index_service', return_value=index):
                app = create_app(settings)
            response = TestClient(app).get('/api/system/status')
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data['retrieval'], {
                'mode': 'hybrid' if hybrid else 'keyword',
                'embedding_model': 'test-embed' if hybrid else None,
                'embedding_version': 'test-embed#1024' if hybrid else None})
            self.assertEqual(data['chat_model'], 'k3')
            self.assertFalse(data['description_enabled'])
            self.assertEqual(data['hitl'], {'enabled': enabled, 'auto_approve_images': 3})
            self.assertEqual(data['features'], dict.fromkeys(('subagents', 'kb_vfs', 'skills'), enabled))
            for secret in ('api_key', 'base_url', 'SECRET-CHAT', 'private-chat.invalid', str(root)):
                self.assertNotIn(secret, response.text)
            app.state.qa_service.close()

    def test_hybrid_enabled_features(self):
        self.check_status(True, True)

    def test_keyword_disabled_features(self):
        self.check_status(False, False)

    def test_independent_features(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            app = create_app(_settings(Path(root), qa_subagents_enabled=True, qa_skills_enabled=False,
                                       qa_kb_vfs_enabled=True))
            self.assertEqual(TestClient(app).get('/api/system/status').json()['features'],
                             {'subagents': True, 'skills': False, 'kb_vfs': True})
            app.state.qa_service.close()
