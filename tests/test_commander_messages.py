import json
from types import SimpleNamespace

from foundry_runtime.app_server_transport import AppServer


def test_message_view_keeps_question_and_deduplicates(tmp_path):
    log = tmp_path/'dispatch.log'
    with log.open('w', encoding='utf-8') as stream:
        app = SimpleNamespace(_log_handle=stream, _message_items=set(), _log=lambda text: None)
        item = {'id': 'question-1', 'text': '请统筹裁决合同冲突；我继续独立测试。',
                'phase': 'commentary', 'questions': [{'question': 'Which contract?'}]}
        params = {'threadId': 'root', 'turnId': 'turn'}
        AppServer._project_message(app, params, item)
        AppServer._project_message(app, params, item)
        AppServer._project_message(app, params, {**item, 'id': {'malformed': True}})
    lines = log.with_suffix('.messages.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result['text'] == item['text'] and result['questions'] == item['questions']
    assert result['thread_id'] == 'root'


def test_non_io_projection_error_does_not_escape(tmp_path):
    messages=[]
    with (tmp_path/'dispatch.log').open('w',encoding='utf-8') as stream:
        app=SimpleNamespace(_log_handle=stream,_message_items=set(),_log=messages.append)
        AppServer._project_message(app,{'threadId':'root','turnId':'turn'},
            {'id':'bad','text':'question','questions':object()})
    assert messages and 'projection unavailable' in messages[0]
