import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serein.api.gateway import routes
from serein.core import Store
from serein.core.reader import Reader
from serein.recall.rendering import render
from serein.recall.source_refs import message_refs


@pytest.mark.parametrize('kind', ['scene', 'event'])
def test_hook_memory_id_reads_all_bound_originals_without_exposing_source_ids(tmp_path, kind):
    database = tmp_path / 'memory.db'
    with Store(database) as store:
        store.create('memory', kind, '那段记忆', '记忆正文。')
        for message, session in [('7', '24'), ('8', '24'), ('7', '25')]:
            source = store.add_source(json.dumps(['assistant_bridge', session, message]),
                '不应注入的绑定原文', metadata={'source_system': 'assistant_bridge', 'session_id': session, 'message_id': message})
            store.bind('memory', source)
        no_id = store.add_source('legacy-opaque-key', '也不应注入的匿名原文')
        store.bind('memory', no_id)
        removed = store.add_source('removed', '已经解绑的原文', metadata={'message_id': '999'})
        store.bind('memory', removed)
        store.conn.execute('UPDATE evidence_bindings SET active=0 WHERE source_id=?', (removed,))
    with Reader(database) as reader:
        obj = reader.read('memory', with_evidence=False)
        assert obj['evidence'] == []
        result = render([{'kind': kind, 'id': 'memory', 'object': obj}], reader=reader)
    card = result['cards'][0]
    assert card['id'] == f'{kind}:memory'
    assert 'source_refs' not in card
    with Reader(database) as reader:
        expanded = reader.read(card['id'], with_evidence=True)
    assert len(expanded['evidence']) == 4
    assert expanded['document']['body_md'] == '记忆正文。'
    assert '已经解绑的原文' not in json.dumps(expanded, ensure_ascii=False)
    app = FastAPI()
    app.include_router(routes(SimpleNamespace(recall=lambda *a, **kw: {**result, 'selected_refs': [f'{kind}:memory']}), []))
    with TestClient(app) as client:
        data = client.post('/api/hook/recall', json={'query': '那段记忆'}).json()
    assert data['cards'][0]['id'] == card['id']
    assert 'source_refs' not in data['cards'][0]
    for context in (result['context'], result['additional_context'], data['additional_context']):
        assert card['id'] in context
        assert 'source_refs' not in context and 'message_ids' not in context
        # The read-on-demand instruction may say "绑定原文"; it is not evidence.
        for original in ('不应注入的绑定原文', '也不应注入的匿名原文', '已经解绑的原文'):
            assert original not in context
        assert '999' not in context and 'legacy-opaque-key' not in context


def test_missing_original_id_is_omitted_and_duplicates_do_not_expand_context():
    evidence = [{'source_id': 'generated_internal_hash', 'source_key': 'opaque', 'metadata': {}}]
    obj = {'document': {'title': '无 ID', 'body_md': '正文', 'metadata': {}}, 'evidence': evidence}
    result = render([{'kind': 'scene', 'id': 'memory', 'object': obj}])
    assert 'source_refs' not in result['cards'][0]
    assert 'source_refs:' not in result['context'] + result['additional_context']
    raw = {'source_key': '["codex","task-1","message-7"]', 'metadata': {}}
    assert message_refs([raw, raw]) == [{'source_system': 'codex', 'conversation_id': 'task-1', 'message_ids': ['message-7']}]
    assert message_refs([{'source_key': raw['source_key'], 'metadata': {'thread_id': 'task-1'}}]) == [
        {'thread_id': 'task-1', 'source_system': 'codex', 'message_ids': ['message-7']}]
