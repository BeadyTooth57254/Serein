import json

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.application import Services
from serein.compat.diaries import Diaries
from serein.compat.germany.diary_store import DiaryLockedError
from serein.config import Settings
from serein.core import Store
from serein.core.reader import Reader
from serein.core.writer import Writer
from serein.deployment import save_settings


def settings_for(tmp_path, canonical=True):
    settings = Settings(tmp_path/'diary.db', writable=True)
    with Store(settings.database):
        pass
    if canonical:
        Diaries(settings.database, initialize=True)
    save_settings(settings.database, {'pipeline': {'auto_enabled': False}})
    return settings


@pytest.mark.parametrize('canonical', [False, True])
def test_public_mcp_diary_create_retry_edit_comment_delete(tmp_path, canonical):
    settings = settings_for(tmp_path, canonical)
    headers = {'Authorization':'Bearer test', 'Accept':'application/json, text/event-stream'}
    with TestClient(create_app(settings, token='test', live=True), headers=headers) as client:
        def rpc(method, params):
            response = client.post('/mcp', json={'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params})
            assert response.status_code == 200
            return response.json()['result']

        def call(name, args, error=False):
            result = rpc('tools/call', {'name':name, 'arguments':args})
            assert bool(result.get('isError')) is error, result
            if error:
                return result
            return result.get('structuredContent') or json.loads(result['content'][0]['text'])

        schema = next(t for t in rpc('tools/list', {})['tools'] if t['name']=='write_diary')['inputSchema']
        assert 'expected_revision' not in schema['required']
        assert 'favorite' not in schema['properties']
        draft = {'operation_id':'new', 'kind':'diary', 'author':'ai', 'day':'2026-09-14', 'body_md':'合成日记正文', 'title':'合成标题'}
        created = call('write_diary', draft)
        assert created['revision'] == 1
        key = int(created['id'])
        assert call('write_diary', draft)['id'] == str(key)
        call('write_diary', {**draft, 'body_md':'重试不能换正文'}, error=True)
        edit = {**draft, 'operation_id':'edit', 'entry_id':key, 'body_md':'合成修改正文'}
        call('write_diary', edit, error=True)
        edit['expected_revision'] = 1
        assert call('write_diary', edit)['revision'] == 2
        assert call('write_diary', edit)['revision'] == 2
        call('write_diary', {**edit, 'operation_id':'stale'}, error=True)
        call('write_diary', {**edit, 'operation_id':'author', 'expected_revision':2, 'author':'user'}, error=True)
        comment = {'operation_id':'comment', 'entry_id':key, 'author':'user', 'body_md':'合成评论'}
        comment_id = call('comment_diary', comment)['id']
        assert call('comment_diary', comment)['id'] == comment_id
        with Reader(settings.database) as reader:
            entry = reader.read(f'diary:{key}')
            assert entry['document']['body_md'] == '合成修改正文'
            assert len(entry['comments']) == 1
        if canonical:
            assert client.get(f'/diaries/{key}').json()['content'] == '合成修改正文'
            assert len(Diaries(settings.database).comments(key)['comments']) == 1
        delete = {'operation_id':'delete', 'entry_id':key, 'expected_revision':2}
        assert call('delete_diary', delete)['revision'] == 3
        assert call('delete_diary', delete)['revision'] == 3
        with Reader(settings.database) as reader:
            assert reader.read(f'diary:{key}')['readable'] is False
        replacement = call('write_diary', {**draft, 'operation_id':'replacement', 'body_md':'同日重新写下'})
        assert replacement['id'] != str(key)
        call('comment_diary', {**comment, 'operation_id':'deleted-comment'}, error=True)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM diary_entries').fetchone()[0] == 2
        assert store.conn.execute("SELECT count(*) FROM diary_entries WHERE visibility='deleted'").fetchone()[0] == 1
        assert store.conn.execute("SELECT count(*) FROM diary_entries WHERE visibility='active'").fetchone()[0] == 1
        assert store.conn.execute('SELECT count(*) FROM write_receipts').fetchone()[0] == 5
        if canonical:
            assert store.conn.execute('SELECT revision FROM diaries WHERE id=?', (key,)).fetchone()[0] == 3
            assert store.conn.execute('SELECT count(*) FROM diary_revisions').fetchone()[0] == 2
            assert store.conn.execute('SELECT count(*) FROM diary_projection_pending').fetchone()[0] == 0


def test_missing_canonical_diary_projection_does_not_block_later_writes(tmp_path):
    settings = settings_for(tmp_path)
    old=Diaries(settings.database).create(content='旧正文',date='2026-09-14',title='旧页',author='ai')
    Diaries(settings.database).comment(old['id'],content='旧留言',author='user')
    with Store(settings.database) as store,store.transaction(immediate=True):
        store.conn.execute('DELETE FROM diaries WHERE id=?',(old['id'],))
    with Store(settings.database,read_only=True) as store:
        assert [row[0] for row in store.conn.execute('SELECT id FROM diary_projection_pending')]==[old['id']]
        assert store.conn.execute('SELECT 1 FROM diary_entries WHERE id=?',(old['id'],)).fetchone()
    draft={'kind':'diary','author':'ai','day':'2026-09-14','body_md':'新正文','title':'新页'}
    created=Services(settings).write('after-missing','diary_save',draft)
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM diary_projection_pending').fetchone()[0]==0
        assert store.conn.execute('SELECT 1 FROM diary_entries WHERE id=?',(old['id'],)).fetchone() is None
        assert store.conn.execute('SELECT 1 FROM diary_comments WHERE entry_id=?',(old['id'],)).fetchone() is None
        assert store.conn.execute('SELECT 1 FROM diary_entries WHERE id=?',(created['id'],)).fetchone()


def test_projection_failure_rolls_back_canonical_diary_and_receipt(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    import serein.compat.diaries as module
    def fail(conn):
        raise RuntimeError('synthetic projection failure')
    monkeypatch.setattr(module, 'project_diaries', fail)
    draft = {'kind':'diary', 'author':'ai', 'day':'2026-09-14', 'body_md':'must roll back'}
    with pytest.raises(RuntimeError, match='projection failure'):
        Services(settings).write('rollback', 'diary_save', draft)
    with Store(settings.database, read_only=True) as store:
        for table in ('diaries', 'diary_entries', 'diary_projection_pending', 'write_receipts'):
            assert store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0
    # The generic writer still cannot bypass the canonical writer contract.
    with Writer(settings.database) as writer, pytest.raises(ValueError, match='production Diary writer'):
        writer.execute('bypass', 'diary_save', draft)


def test_canonical_diary_locked_entries_stay_protected(tmp_path):
    settings = settings_for(tmp_path)
    services = Services(settings)
    draft = {'kind':'darkroom', 'author':'ai', 'day':'2026-09-14', 'body_md':'合成锁定正文', 'unlock_at':'2099-01-01T00:00:00+08:00'}
    saved = services.write('locked', 'diary_save', draft)
    key = int(saved['id'])
    with Reader(settings.database) as reader:
        assert reader.read(f'darkroom:{key}')['readable'] is False
    for action, request in (
        ('diary_save', {**draft, 'entry_id':key, 'expected_revision':1, 'body_md':'must not change'}),
        ('diary_comment', {'entry_id':key, 'author':'ai', 'body_md':'must not comment'}),
        ('diary_delete', {'entry_id':key, 'expected_revision':1}),
    ):
        with pytest.raises(DiaryLockedError, match='locked'):
            services.write(action, action, request)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT revision FROM diaries WHERE id=?', (key,)).fetchone()[0] == 1
        assert store.conn.execute('SELECT count(*) FROM write_receipts').fetchone()[0] == 1
