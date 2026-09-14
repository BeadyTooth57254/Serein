import json
import sqlite3

import pytest

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.notebook import read_entry, resolve_entry
from serein.core.store import Store, encode
from serein.compat.diaries import Diaries
from serein.compat.germany.diary_store import DiaryStore
from serein.legacy_migration.history import scan_history, import_history, run_history_repair
from serein.legacy_migration.scan import scan
from serein.legacy_migration.workflow import Migration


@pytest.fixture
def history_source(tmp_path):
    root = tmp_path / 'old'
    (root / 'buckets/dynamic').mkdir(parents=True)
    (root / 'buckets/dynamic/a.md').write_text('---\nid: a\nname: Source scene\n---\nBody', 'utf-8')
    state = root / 'state'
    diaries = DiaryStore(db_path=state / 'diary.db')
    diaries.create(content='Original diary\r\n', author='user', preserve_content=True)
    diaries.create(content='Locked darkroom', entry_type='darkroom', unlock_at='2099-01-01T00:00:00+08:00')
    diaries.create(content='Deleted darkroom', entry_type='darkroom', source_id='legacy_darkroom:dr_old')
    diaries.comment(1, content='Original comment', author='ai')
    diaries.revise(1, content='Revised diary')
    diaries.delete(3)
    with sqlite3.connect(state / 'diary.db') as db:
        db.execute("INSERT INTO darkroom_sessions(locked_at,unlock_at,diary_id) VALUES ('2026-01-01','2099-01-01T00:00:00+08:00',2)")
    with sqlite3.connect(state / 'window_shadows.sqlite') as db:
        db.execute('CREATE TABLE window_shadows(window_id TEXT PRIMARY KEY, revision_number INTEGER, content TEXT, moment_bucket_ids_json TEXT)')
        db.execute("INSERT INTO window_shadows VALUES ('window_old',2,'Original shadow','[]')")
    (state / 'dreams/logs').mkdir(parents=True)
    for key in ('kept', 'deleted'):
        (state / f'dreams/dream_{key}.md').write_bytes(
            f'---\r\ndream_id: dream_{key}\r\ngenerated_at: "2026-01-01"\r\nsurfaced: true\r\n---\r\nOriginal dream\r\n'.encode())
    (state / 'dreams/logs/events.jsonl').write_text(encode({'event': 'deleted', 'dream_id': 'dream_deleted',
                                                       'deleted_at': '2026-01-02'}) + '\n', 'utf-8')
    (state / 'darkroom').mkdir()
    (state / 'darkroom/entries.jsonl').write_text(encode({'id': 'dr_old', 'note': 'Deleted darkroom', 'visibility': 'active', 'created_at':'2026-01-01T00:00:00+08:00'}) + '\n', 'utf-8')
    (state / 'darkroom/state.json').write_text('{"last_entry_id":"dr_old"}', 'utf-8')
    return root


@pytest.fixture
def target(tmp_path):
    settings = Settings(tmp_path / 'new/serein.db', writable=True)
    initialize(settings)
    return settings


def test_scan_includes_history_without_changing_scene_fingerprint(history_source):
    plan = scan(history_source)
    assert not plan['errors']
    summary = plan['summary']['history']
    assert [summary[k] for k in ('diaries','darkroom','comments','revisions','sessions','dreams','shadows')] == [1,2,1,2,1,2,1]
    (history_source / 'state/dreams/dream_more.md').write_text('---\ndream_id: dream_more\n---\nMore', 'utf-8')
    changed = scan(history_source)
    assert changed['fingerprint'] == plan['fingerprint']
    assert changed['history']['fingerprint'] != plan['history']['fingerprint']


def test_history_import_preserves_raw_api_tables_projections_and_locks(history_source, target):
    plan = scan(history_source)
    m = Migration(target, plan, {'user_name':'User','ai_name':'AI','aliases':[]})
    try:
        m.backup()
        result = m.import_history()
    finally:
        m.close()
    assert result['id_maps']['diaries'] == {'1':1, '2':2, '3':3}
    assert not result['held']
    with Store(target.database, read_only=True) as store:
        assert read_entry(store, 1)['entry']['content'] == 'Revised diary'
        assert read_entry(store, 1)['comments'][0]['content'] == 'Original comment'
        assert resolve_entry(store, 2)['resolution'] == 'locked'
        assert read_entry(store, 2)['entry'] is None
        assert resolve_entry(store, 'dr_old')['resolution'] == 'deleted'
        assert store.conn.execute('SELECT count(*) FROM diary_history').fetchone()[0] == 2
        assert store.conn.execute('SELECT entry_id FROM diary_sessions').fetchone()[0] == 2
        assert store.conn.execute("SELECT body_md FROM historical_works WHERE id='dream_kept'").fetchone()[0] == 'Original dream\r\n'
        assert store.conn.execute("SELECT 1 FROM deletions WHERE document_id='dream_deleted'").fetchone()
        assert store.conn.execute("SELECT body_md FROM historical_works WHERE kind='shadow'").fetchone()[0] == 'Original shadow'
        assert store.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert Diaries(target.database).comments(1)['comments'][0]['content'] == 'Original comment'


def test_colliding_ids_are_remapped_and_repeat_does_not_resurrect(history_source, target):
    current = Diaries(target.database)
    current.create(content='New diary', author='user')
    current.comment(1, content='New comment')
    current.revise(1, content='New edited diary')
    plan = scan(history_source)
    result = run_history_repair(target, plan)
    mapped = result['id_maps']['diaries']['1']
    assert mapped > 3
    assert current.read(diary_id=1)['diaries'][0]['content'] == 'New edited diary'
    assert current.comments(1)['comments'][0]['content'] == 'New comment'
    assert current.comments(mapped)['comments'][0]['content'] == 'Original comment'
    assert result['id_maps']['comments']['1'] != 1
    assert result['id_maps']['diary_revisions']['1'] != 1
    current.delete(mapped)
    with Store(target.database) as store:
        store.record_deletion('dream_kept', '2026-09-13', {})
    repeated = run_history_repair(target, plan)
    assert repeated['repeated']
    with Store(target.database, read_only=True) as store:
        assert resolve_entry(store, mapped)['resolution'] == 'deleted'
        assert store.conn.execute('SELECT count(*) FROM diaries').fetchone()[0] == 4
        assert store.conn.execute("SELECT 1 FROM deletions WHERE document_id='dream_kept'").fetchone()
    with sqlite3.connect(result['backup']) as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT count(*) FROM diaries').fetchone()[0] == 1


def test_source_changes_are_rejected_and_failed_archive_rolls_back_diaries(history_source, target):
    plan = scan(history_source)
    (history_source / 'state/dreams/dream_kept.md').write_text('broken', 'utf-8')
    with pytest.raises(ValueError, match='预览后'):
        run_history_repair(target, plan)
    broken = scan(history_source)
    with pytest.raises(ValueError, match='dream_id'):
        run_history_repair(target, broken)
    with Store(target.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM diaries').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM import_records').fetchone()[0] == 0
        assert store.conn.execute("SELECT count(*) FROM background_state WHERE name LIKE 'legacy-history:%'").fetchone()[0] == 0


def test_changed_already_imported_source_never_overwrites(history_source, target):
    plan = scan(history_source)
    run_history_repair(target, plan)
    DiaryStore(db_path=history_source / 'state/diary.db').revise(1, content='Changed after original snapshot')
    with pytest.raises(ValueError, match='已变化'):
        run_history_repair(target, scan(history_source))
    assert Diaries(target.database).read(diary_id=1)['diaries'][0]['content'] == 'Revised diary'


def test_missing_optional_sources_warn_and_can_be_supplemented_later(tmp_path, target):
    root = tmp_path / 'old'; root.mkdir()
    source = scan_history(root)
    assert len(source['summary']['warnings']) == 1
    assert import_history(target.database, source, 'old')['empty']
    DiaryStore(db_path=root/'state/diary.db').create(content='Found later')
    result = import_history(target.database, scan_history(root), 'old')
    assert result['id_maps']['diaries'] == {'1':1}


def test_public_json_only_darkroom_imports_rooms_revisions_locks_and_states(tmp_path, target):
    root = tmp_path/'old'; folder = root/'state/darkroom'; folder.mkdir(parents=True)
    rows = [
        {'id':'dr_first','room_id':'room_one','revision':1,'created_at':'2026-01-01T10:00:00+08:00','note':'First body','visibility':'active'},
        {'id':'dr_second','room_id':'room_one','revision':2,'created_at':'2026-01-02T11:00:00+08:00','note':'Latest body','visibility':'active','locked_until':'2099-01-01T00:00:00+08:00'},
        {'id':'dr_archived','created_at':'2026-01-01T10:00:00+08:00','note':'Archived body','visibility':'archived'},
        {'id':'dr_retracted','created_at':'2026-01-01T10:00:00+08:00','note':'Retracted body','visibility':'retracted'},
    ]
    original = ''.join(encode(row)+'\n' for row in rows).encode()
    (folder/'entries.jsonl').write_bytes(original)
    (folder/'releases.jsonl').write_bytes(b'{"id":"rel_old","entry_id":"dr_first"}\n')
    (folder/'state.json').write_bytes(b'{"last_entry_id":"dr_second"}')
    (folder/'backups/deleted').mkdir(parents=True)
    (folder/'backups/deleted/entries.jsonl').write_bytes(b'{"id":"dr_deleted","note":"Deleted backup"}')
    source = scan_history(root)
    assert source['summary']['json_darkroom_rooms'] == 3
    assert source['summary']['darkroom'] == 3 and source['summary']['revisions'] == 1
    assert not source['summary']['warnings']
    current = Diaries(target.database);current.create(content='Existing new diary')
    result = import_history(target.database, source, 'old')
    assert not result['held']
    mapped = result['id_maps']['legacy_darkroom']
    assert mapped['dr_first'] == mapped['dr_second'] > 1
    with Store(target.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM diaries').fetchone()[0] == 4
        assert bytes(store.conn.execute("SELECT content FROM import_records WHERE path='darkroom/entries.jsonl'").fetchone()[0]) == original
        assert store.conn.execute("SELECT content FROM diaries WHERE id=?",(mapped['dr_second'],)).fetchone()[0]=='Latest body'
        assert store.conn.execute('SELECT body_md FROM diary_history').fetchone()[0]=='First body'
        assert read_entry(store,'dr_second')['resolution']=='locked'
        assert read_entry(store,'dr_archived')['resolution']=='archived'
        assert read_entry(store,'dr_retracted')['resolution']=='retracted'
        assert resolve_entry(store,'dr_deleted')['resolution']=='missing'
        assert read_entry(store,1)['entry']['content']=='Existing new diary'
    assert import_history(target.database,source,'old')['repeated']
    payload=Diaries(target.database).search(limit=100)
    for row in payload['diaries']:
        if row['entry_type']=='darkroom':
            assert not row['body_available'] and row['content']==''
            assert not any(text in encode(row) for text in ('Latest body','First body','Archived body','Retracted body'))


def test_public_darkroom_invalid_lock_stops_scan_instead_of_unlocking(tmp_path):
    root=tmp_path/'old';folder=root/'state/darkroom';folder.mkdir(parents=True)
    row={'id':'dr_bad','created_at':'2026-01-01T00:00:00+08:00','note':'Secret','locked_until':'bad-time'}
    (folder/'entries.jsonl').write_text(encode(row), 'utf-8')
    with pytest.raises(ValueError,match='锁定时间'):
        scan_history(root)


def test_public_dream_only_backup_needs_neither_diary_nor_shadow_database(history_source,tmp_path,target):
    root=tmp_path/'dream-only';folder=root/'state/dreams/logs';folder.mkdir(parents=True)
    for relative in ('dreams/dream_kept.md','dreams/dream_deleted.md','dreams/logs/events.jsonl'):
        (root/'state'/relative).write_bytes((history_source/'state'/relative).read_bytes())
    source=scan_history(root)
    assert source['summary']['dreams']==2
    assert not source['summary']['warnings']
    result=import_history(target.database,source,'dream-only')
    assert result['archives']['kinds']=={'dream':2}
    with Store(target.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM diaries').fetchone()[0]==0
        assert store.conn.execute("SELECT 1 FROM deletions WHERE document_id='dream_deleted'").fetchone()


def test_paths_outside_backup_are_not_read(tmp_path):
    root=tmp_path/'backup';root.mkdir()
    outside=tmp_path/'outside';outside.mkdir()
    DiaryStore(db_path=outside/'diary.db').create(content='Outside')
    (root/'config.yaml').write_text('diary:\n  db_path: '+(outside/'diary.db').as_posix()+'\n', 'utf-8')
    source=scan_history(root)
    assert source['tables']['diaries'] == []
    assert any('路径' in item for item in source['summary']['warnings'])


def test_cli_history_repair_requires_unchanged_preview_and_no_models(history_source,target,monkeypatch,capsys):
    from serein.legacy_migration.__main__ import main
    monkeypatch.setattr('serein.legacy_migration.__main__.load_settings',lambda path:target)
    args=['migration','--config','unused','repair-history',str(history_source)]
    monkeypatch.setattr('sys.argv',args)
    main()
    assert '当前仅预览' in capsys.readouterr().out
    with Store(target.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM diaries').fetchone()[0]==0
    monkeypatch.setattr('sys.argv',args+['--apply'])
    main()
    assert Diaries(target.database).read(diary_id=1)['diaries'][0]['content']=='Revised diary'
    DiaryStore(db_path=history_source/'state/diary.db').revise(1,content='Changed')
    with pytest.raises(SystemExit,match='预览不一致'):
        main()


def test_web_workflow_imports_history_before_model_stages(history_source, target, monkeypatch):
    from serein.legacy_migration.web import preview_path, location, run
    from serein.legacy_migration.workflow import Migration
    preview = preview_path(target, str(history_source))
    (location(target,preview['id'])/'options.json').write_text(encode({'user_name':'User','ai_name':'AI','aliases':[]}), 'utf-8')
    monkeypatch.setattr(Migration, 'freeze_configuration', lambda self: None)
    async def tag(self):
        assert Diaries(target.database).read(diary_id=1)['diaries'][0]['content'] == 'Revised diary'
    async def edges(self): pass
    async def refresh(*args, **kwargs):
        assert kwargs['retry_failed'] is True
        return {'cues':{}}
    monkeypatch.setattr(Migration, 'tag_all', tag)
    monkeypatch.setattr(Migration, 'edges', edges)
    monkeypatch.setattr('serein.configured_models.prepare_selected', lambda *a,**kw: {})
    monkeypatch.setattr('serein.configured_models.effective_settings', lambda s:s)
    monkeypatch.setattr('serein.recall.legacy_indexes.refresh', refresh)
    assert run(target,preview['id'])['status'] == 'completed'
