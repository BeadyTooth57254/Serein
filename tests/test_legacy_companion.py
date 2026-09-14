from contextlib import closing
from datetime import datetime
import json
import sqlite3

import pytest

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import read_settings
from serein.compat.memo_store import ReminderStore, LOCAL_TZ
from serein.compat.persona_engine import PersonaStateEngine
from serein.legacy_migration.scan import scan
from serein.legacy_migration.companion import scan_companion, import_companion
from serein.legacy_migration.workflow import Migration


@pytest.fixture
def companion(tmp_path):
    root = tmp_path / 'old'
    (root / 'buckets/dynamic').mkdir(parents=True)
    (root / 'state').mkdir()
    (root / 'config.yaml').write_text('persona:\n  profile_id: old-profile\ngateway:\n  default_session_id: old-main\n', 'utf-8')
    persona = PersonaStateEngine({'serein_database': root / 'state/persona_state.db',
                                 'persona': {'enabled': False, 'profile_id': 'old-profile'}})
    stamp = datetime(2026, 9, 11, 10, tzinfo=LOCAL_TZ)
    persona._ensure_global_state(stamp)
    persona._ensure_session_state('old-main', stamp)
    with closing(persona._connect()) as conn, conn:
        conn.execute("UPDATE persona_global_state SET trust=.81,affinity=.73")
        conn.execute("UPDATE persona_session_state SET tenderness=.62,inner_thought='还想聊聊那本书。',residue='安静',libido=.2")
        conn.execute("INSERT INTO persona_events(profile_id,session_id,message_hash,exchange_hash,event_type,inner_thought,created_at) VALUES ('old-profile','old-main','m1','x1','affection','记得一起读书。',?)", (stamp.isoformat(),))
        conn.execute("INSERT INTO persona_exchange_log(profile_id,session_id,exchange_hash,created_at) VALUES ('old-profile','old-main','x1',?)", (stamp.isoformat(),))
    memos = ReminderStore({'serein_database': root / 'state/reminders.sqlite'})
    for key, status in [('active', 'active'), ('done', 'done'), ('archived', 'archived')]:
        memos.create(reminder_id=key, title='读书', content='记得带上那本书。', session_id='old-main',
                     interval_rounds=6, daily_limit=2, max_injections=9, end_at='2099-01-01')
        with closing(memos._connect()) as conn, conn:
            conn.execute('UPDATE reminders SET status=?,reminder_count=4,last_reminded_round=17,daily_reminder_date=?,daily_reminder_count=1 WHERE id=?', (status, stamp.date().isoformat(), key))
    with closing(sqlite3.connect(root / 'buckets/gateway_state.db')) as conn, conn:
        conn.execute('CREATE TABLE request_rounds(session_id TEXT,round_id INTEGER)')
        conn.executemany('INSERT INTO request_rounds VALUES (?,?)', [('old-main', 20), ('other-window', 8)])
    settings = Settings(tmp_path / 'new/serein.db', tmp_path / 'new/index.sqlite', writable=True)
    initialize(settings)
    return root, settings


def test_companion_migration_preserves_state_counts_and_retry_after_edits(companion):
    root, settings = companion
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file() and p.suffix in ('.db', '.sqlite')}
    plan = scan(root)
    assert not plan['errors'] and plan['summary']['companion']['rows']['reminders'] == 3
    migration = Migration(settings, plan, {'user_name': 'Reader', 'ai_name': 'Guide'})
    try:
        migration.backup()
        result = migration.import_companion()
        assert result['rows']['persona_events'] == 1 and result['rounds']['main'] == 20
        with Store(settings.database) as store:
            assert store.conn.execute('SELECT trust FROM persona_global_state WHERE profile_id="default"').fetchone()[0] == .81
            row = store.conn.execute('SELECT * FROM persona_session_state').fetchone()
            assert row['session_id'] == 'main' and row['tenderness'] == .62 and row['inner_thought'] == '还想聊聊那本书。'
            assert store.conn.execute('SELECT count(*) FROM persona_exchange_log').fetchone()[0] == 1
            assert json.loads(store.conn.execute("SELECT value_json FROM background_state WHERE name='feature_round:main'").fetchone()[0]) == 20
        memos = ReminderStore({'serein_database': settings.database})
        row = memos.get('active')
        assert row['session_id'] == 'main' and row['reminder_count'] == 4 and row['last_reminded_round'] == 17
        assert row['daily_reminder_count'] == 1 and row['max_injections'] == 9 and row['end_at'].startswith('2099')
        assert memos.get('done')['status'] == 'done' and memos.get('archived')['status'] == 'archived'
        moment = datetime(2026, 9, 11, 12, tzinfo=LOCAL_TZ)
        assert not memos.due(session_id='main', round_id=21, now=moment)
        assert [r['id'] for r in memos.due(session_id='main', round_id=23, now=moment)] == ['active']
        memos.set_status('active', 'done')
        assert migration.import_companion()['status'] == 'idempotent'
        assert memos.get('active')['status'] == 'done'
        assert migration.report()['companion'] == {'done': 1}
        assert not read_settings(settings.database)['features']['persona']
        assert not read_settings(settings.database)['features']['memos']
        assert all(path.read_bytes() == contents for path, contents in before.items())
    finally:
        migration.close()


def test_source_drift_and_target_collision_never_partially_overwrite(companion):
    root, settings = companion
    source = scan_companion(root)
    target = ReminderStore({'serein_database': settings.database})
    target.create(reminder_id='done', title='新库自己的备忘', content='保留')
    with pytest.raises(ValueError, match='冲突'):
        import_companion(settings.database, source, str(root))
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM persona_global_state').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM reminders').fetchone()[0] == 1
        store.conn.execute('DELETE FROM reminders')
    import_companion(settings.database, source, str(root))
    with closing(sqlite3.connect(root / 'state/reminders.sqlite')) as conn, conn:
        conn.execute("UPDATE reminders SET content='旧备份被改了' WHERE id='active'")
    with pytest.raises(ValueError, match='来源已改变'):
        import_companion(settings.database, scan_companion(root), str(root))


def test_companion_addition_keeps_old_memory_fingerprint(companion):
    root, settings = companion
    first = scan(root)
    with closing(sqlite3.connect(root / 'state/reminders.sqlite')) as conn, conn:
        conn.execute("UPDATE reminders SET reminder_count=5 WHERE id='active'")
    second = scan(root)
    assert first['fingerprint'] == second['fingerprint']
    assert first['companion']['fingerprint'] != second['companion']['fingerprint']
    migration = Migration(settings, first, {'user_name': 'Reader', 'ai_name': 'Guide'})
    try:
        with pytest.raises(ValueError, match='扫描后'):
            migration.import_companion()
    finally:
        migration.close()


def test_external_configured_paths_are_not_opened(tmp_path):
    root = tmp_path / 'backup'; root.mkdir()
    outside = tmp_path / 'not-a-database.db'; outside.write_text('do not open')
    (root / 'config.yaml').write_text(f'persona:\n  db_path: {outside.as_posix()}\n', 'utf-8')
    result = scan_companion(root)
    assert result['files'] == [] and result['summary']['warnings']


def test_runtime_profile_selection_and_missing_round_fallback(companion):
    root, _ = companion
    (root / 'config.yaml').write_text('persona:\n  profile_id: stale\n', 'utf-8')
    (root / 'state/config.runtime.yaml').write_text('persona:\n  profile_id: old-profile\ngateway:\n  default_session_id: old-main\n', 'utf-8')
    (root / 'buckets/gateway_state.db').unlink()
    result = scan_companion(root)
    assert result['profile'] == 'old-profile' and result['rounds']['old-main'] == 17
    assert any('下限' in warning for warning in result['summary']['warnings'])


def test_older_memo_columns_use_defaults_and_are_visible_in_api(companion):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    root, settings = companion
    with closing(sqlite3.connect(root / 'state/reminders.sqlite')) as conn, conn:
        for column in ('daily_limit', 'daily_reminder_count', 'daily_reminder_date', 'max_injections'):
            conn.execute(f'ALTER TABLE reminders DROP COLUMN {column}')
    import_companion(settings.database, scan_companion(root), str(root))
    with TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'}) as client:
        persona = client.get('/v1/companion/persona').json()
        assert persona['relationship']['trust'] == .81 and persona['session_id'] == 'main'
        assert len(persona['events']) == 1 and not persona['enabled']
        memos = client.get('/v1/companion/memos').json()['items']
        assert len(memos) == 3 and all(row['reminder_count'] == 4 for row in memos)
        assert all(row['daily_limit'] == 1 for row in memos)


def test_multiple_profiles_without_selection_are_reported(companion):
    root, _ = companion
    (root / 'config.yaml').write_text('{}', 'utf-8')
    with closing(sqlite3.connect(root / 'state/persona_state.db')) as conn, conn:
        conn.execute("INSERT INTO persona_events(profile_id,session_id,message_hash,created_at) VALUES ('another','a','b','2026-01-01')")
    result = scan(root)
    assert result['errors'] and '多个档案' in result['errors'][0]['error']
