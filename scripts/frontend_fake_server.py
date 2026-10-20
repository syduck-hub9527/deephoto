"""Disposable browser fixture. No .env, production data or external models.

Run: .venv/Scripts/python.exe scripts/frontend_fake_server.py [--seed-history] [--slow-stream]
Questions containing 审批 trigger real inspect_image HITL; 失败 triggers a graph
failure. All other questions read the seeded chunk and answer with a citation.
"""
import io
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
import uvicorn
import fitz
from PIL import Image
from langchain_core.messages import HumanMessage, ToolMessage
from support.fakes import ScriptedFakeChatModel
from deephoto import repo
from deephoto.api.app import create_app
from deephoto.db import connect
from deephoto.security import LOCAL_CTX
from test_upload_route import _settings


def main():
    with tempfile.TemporaryDirectory(prefix='yunhuan-ui-', ignore_cleanup_errors=True) as directory:
        settings = _settings(Path(directory), mineru_api_key=None,
                             qa_persistence_enabled=True, qa_middleware_enabled=True,
                             qa_memory_enabled=True, qa_hitl_enabled=True,
                             qa_hitl_auto_approve_images=0)
        app = create_app(settings)
        conn = connect(settings.db_path)
        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((72, 72), 'Yunhuan UI fixture - device structure and parameters')
        key, digest = app.state.store.put(pdf.tobytes(), 'sources', 'application/pdf', ext='pdf')
        pdf.close()
        doc = repo.insert_document(conn, tenant_id=LOCAL_CTX.tenant_id, owner_id=LOCAL_CTX.user_id,
                                   filename='器件结构测试.pdf', source_object_key=key, sha256=digest,
                                   ingestion_version='v2', source_format='pdf', parse_engine='mineru')
        repo.update_document_status(conn, doc, 'ready', page_count=1)
        image = io.BytesIO()
        Image.new('RGB', (160, 90), '#2f9e63').save(image, format='PNG')
        image_key, image_hash = app.state.store.put(image.getvalue(), 'images', 'image/png', ext='png')
        asset = repo.get_or_create_asset(conn, tenant_id=LOCAL_CTX.tenant_id, sha256=image_hash,
                                         object_key=image_key, width=160, height=90, mime_type='image/png')
        occurrence = repo.insert_occurrence(conn, tenant_id=LOCAL_CTX.tenant_id, document_id=doc,
                                            ingestion_version='v2', image_asset_id=asset, page_number=1,
                                            bbox=None, figure_number='1', caption='器件结构测试图',
                                            extraction_method='embedded_bitmap', needs_review=False)
        chunk = repo.insert_chunk(conn, tenant_id=LOCAL_CTX.tenant_id, document_id=doc,
                                  ingestion_version='v2', section='器件结构',
                                  text='器件由电极和功能层构成。实验温度为 37 摄氏度。',
                                  page_start=1, page_end=1, paragraph_ids=[],
                                  referenced_image_ids=[occurrence], nearby_image_ids=[])
        app.state.index_service.upsert_document(conn, doc)
        conn.commit()

        model_delay = 2 if '--slow-stream' in sys.argv else .25

        class BrowserFake(ScriptedFakeChatModel):
            def _next_message(self):
                messages = self.requests[-1]
                question = str(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage)))
                if '失败' in question:
                    raise RuntimeError('浏览器测试构造的失败')
                last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
                tools = [m for m in messages[last_human + 1:] if isinstance(m, ToolMessage)]
                if not tools:
                    step = {'text': '先检索文档并核对器件结构。', 'tool': 'read_chunk', 'args': {'chunk_id': chunk}}
                elif '审批' in question and not any(m.name == 'inspect_image' for m in tools):
                    step = {'text': '接下来查看器件原图。', 'tool': 'inspect_image', 'args': {'image_occurrence_id': occurrence}}
                else:
                    previous = [str(m.content) for m in messages[:last_human] if isinstance(m, HumanMessage)]
                    step = {'text': f'器件由电极和功能层构成。[chunk:{chunk}]\n\n' +
                            (f'上文问题：{previous[-1]}\n\n' if previous else '') +
                            (f'[image:{occurrence}]' if '审批' in question else '实验温度为 **37°C**。')}
                self.script.append(step)
                time.sleep(model_delay)
                return super()._next_message()

        app.state.qa_service._chat_model = BrowserFake(script=[], model_name='yunhuan-ui-fake', ls_provider='openai')
        if '--seed-history' in sys.argv:
            model_delay = 0
            app.state.qa_service.answer(conn, LOCAL_CTX, '分页验证旧会话')
            for number in range(31):
                app.state.qa_service.answer(conn, LOCAL_CTX, f'分页测试会话 {number + 1}')
            outside = app.state.qa_service.list_sessions(conn, LOCAL_CTX, limit=30, offset=30)[0]['session_id']
            print(f'Session outside first page: http://127.0.0.1:8765/#/s/{outside}', flush=True)
            model_delay = 2 if '--slow-stream' in sys.argv else .25
        print(f'Disposable fixture ready: http://127.0.0.1:8765 doc={doc}', flush=True)
        try:
            uvicorn.run(app, host='127.0.0.1', port=8765, log_level='warning')
        finally:
            conn.close()


if __name__ == '__main__':
    main()
