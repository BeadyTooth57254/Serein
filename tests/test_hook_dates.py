from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from serein.api.gateway import routes
from serein.recall.rendering import render


def rendered(kind, metadata):
    doc = {'body_md': '过去留下的原文摘要。', 'title': '记忆日期测试',
           'metadata': metadata, 'created_at': '2026-09-07T00:00:00Z'}
    result = render([{'kind': kind, 'id': 'dated', 'object': {'document': doc}}])
    return {**result, 'selected_refs': [f'{kind}:dated']}


def test_hook_keeps_cross_midnight_source_times_in_cards_and_injection():
    result = rendered('event', {'local_date': '2026-09-06', 'local_end_date': '2026-09-07',
        'source_started_at': '2026-09-06 15:59:12', 'source_ended_at': '2026-09-06T16:02:34Z'})
    app = FastAPI()
    app.include_router(routes(SimpleNamespace(recall=lambda *a, **kw: result), []))
    with TestClient(app) as client:
        response = client.post('/api/hook/recall', json={'query': '还记得那次吗', 'recall_mode': 'full'})
    assert response.status_code == 200
    data = response.json(); card = data['cards'][0]
    assert card['date'] == '2026-09-06' and card['end_date'] == '2026-09-07'
    assert card['started_at'] == '2026-09-06T23:59:12+08:00'
    assert card['ended_at'] == '2026-09-07T00:02:34+08:00'
    assert card['date_basis'] == 'source_messages'
    assert card['timezone'] == 'Asia/Shanghai'
    for text in (data['additional_context'], result['additional_context']):
        assert 'started_at: 2026-09-06T23:59:12+08:00' in text
        assert 'ended_at: 2026-09-07T00:02:34+08:00' in text
    assert data['notes'] == data['cards'] and not data['injected']


def test_offset_is_not_applied_twice_and_local_only_times_keep_precision():
    card = rendered('event', {'source_started_at': '2026-09-06T23:59:12+08:00'})['cards'][0]
    assert card['started_at'] == '2026-09-06T23:59:12+08:00'
    assert card['date'] == '2026-09-06'
    card = rendered('event', {'local_date': '2026-09-06', 'local_end_date': '2026-09-07',
        'local_start_time': '23:59', 'local_end_time': '00:02'})['cards'][0]
    assert card['local_start'] == '2026-09-06 23:59'
    assert card['local_end'] == '2026-09-07 00:02'
    assert 'started_at' not in card


def test_scene_date_is_not_replaced_by_created_or_modified_date():
    result = rendered('scene', {'event_date': '2026-08-01', 'created': '2026-08-05', 'updated_at': '2026-09-07'})
    assert result['cards'][0]['date'] == '2026-08-01'
    assert 'date: 2026-08-01' in result['full_additional_context']
    missing = rendered('scene', {'created': '2026-08-05', 'updated_at': '2026-09-07'})
    assert 'date' not in missing['cards'][0]
    assert missing['cards'][0]['recorded_at'] == '2026-08-05'
    assert 'recorded_at: 2026-08-05' in missing['context']
