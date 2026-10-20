"""使用临时合成知识库与现有问答配置验证真实模型流式正文。

只发起一轮业务问答（其中工具循环由原有框架与预算管理）。不调用解析、
图片描述或嵌入服务。运行前输出问答服务主机与模型名，不输出凭据。

Run: .venv/Scripts/python.exe scripts/smoke_stream_preamble.py --report <path.json>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from deephoto import repo
from deephoto.agent.knowledge import KnowledgeService
from deephoto.agent.qa import QAService
from deephoto.config import load_settings
from deephoto.db import connect, init_db
from deephoto.indexing.service import IndexService
from deephoto.security import LOCAL_CTX
from deephoto.storage import ObjectStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--question', default='请检索测试资料，比较甲型与乙型电路的供电电压、输出延迟，并标注出处。')
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    original = load_settings()
    if not original.moonshot_api_key:
        raise SystemExit('未配置问答凭据，未执行真实模型冒烟')
    result = {'model': original.chat_model, 'host': urlparse(original.moonshot_base_url).hostname,
              'passed': False, 'request_scope': 'one question against a temporary synthetic document'}
    print(json.dumps({'model': result['model'], 'host': result['host'], 'scope': result['request_scope']}, ensure_ascii=False), flush=True)
    with tempfile.TemporaryDirectory(prefix='stream-preamble-smoke-', ignore_cleanup_errors=True) as directory:
        settings = replace(original, data_dir=Path(directory))
        init_db(settings.db_path)
        conn = connect(settings.db_path)
        store = ObjectStore(settings.object_dir)
        document_text = ('# 测试电路资料\n\n'
            '甲型电路的供电电压为 5 V，输出延迟为 12 ns。\n\n'
            '乙型电路的供电电压为 3.3 V，输出延迟为 8 ns。')
        key, digest = store.put(document_text.encode(), 'sources', 'text/plain', ext='txt')
        document = repo.insert_document(conn, tenant_id=LOCAL_CTX.tenant_id, owner_id=LOCAL_CTX.user_id,
            filename='流式回答冒烟测试资料.txt', source_object_key=key, sha256=digest,
            ingestion_version=settings.ingestion_version, source_format='txt', locator_kind='section', parse_engine='local')
        repo.update_document_status(conn, document, 'ready', page_count=2)
        for page, text in enumerate(document_text.split('\n\n')[1:], 1):
            repo.insert_chunk(conn, tenant_id=LOCAL_CTX.tenant_id, document_id=document,
                ingestion_version=settings.ingestion_version, section=f'测试电路 {page}', text=text,
                page_start=page, page_end=page, paragraph_ids=[], referenced_image_ids=[], nearby_image_ids=[])
        index = IndexService()
        index.upsert_document(conn, document)
        conn.commit()
        qa = QAService(settings, KnowledgeService(store, index))
        session_id = None
        try:
            events = []
            for event in qa.answer_stream(LOCAL_CTX, args.question, document):
                if event['type'] == 'session':
                    session_id = event['session_id']
                events.append(event)
            done = [e for e in events if e['type'] == 'done']
            steps = [e for e in events if e['type'] == 'step']
            errors = [e for e in events if e['type'] in ('error', 'warning')]
            result.update(event_types=[e['type'] for e in events], step_count=len(steps),
                          done_count=len(done), error_count=len(errors), session_id=session_id)
            assert not errors, '真实模型产生 error / warning 事件'
            assert len(done) == 1 and done[0].get('answer', '').strip(), '缺少唯一的非空最终回答'
            answer = done[0]['answer']
            result.update(answer=answer, citation_count=len(done[0].get('citations', [])),
                          tool_names=[e['tool'] for e in done[0].get('tool_trace', {}).get('events', [])])
            assert session_id and done[0].get('session_id') == session_id, '会话 ID 缺失或发生变化'
            assert done[0].get('session_status') == 'ready', '会话没有健康完成'
            history = qa.session_history(conn, LOCAL_CTX, session_id)
            result['history_matches'] = history['messages'][-1]['answer'] == answer
            assert result['history_matches'], '实时正文与 checkpoint 历史不一致'
            assert result['citation_count'] > 0, '回答未提供已校验出处'
            assert any(name in {'search_knowledge', 'read_chunk', 'read_file', 'grep'} for name in result['tool_names']), '没有可观察的检索工具调用'
            assert steps, '本次真实模型未产生 step，不能算作混合文字 / 工具调用冒烟通过'
            assert all(not answer.startswith(e['text'].strip()) for e in steps if e['text'].strip()), '过程说明仍位于正文开头'
            assert not answer.startswith(('先检索', '先补充检索', '检索到文档，先')), '正文仍以过程说明开头'
            result['passed'] = True
        except Exception as exc:
            result['failure_type'] = type(exc).__name__
            # 只记录本脚本的固定断言说明，不保存 provider 异常载荷或推理文本。
            if isinstance(exc, AssertionError):
                result['failure'] = str(exc)
        finally:
            try:
                if session_id:
                    qa.delete_session(LOCAL_CTX, session_id)
            finally:
                qa.close()
                conn.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('answer', 'event_types', 'session_id')}, ensure_ascii=False), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
