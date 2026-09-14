import asyncio
import json
from dataclasses import replace

import pytest

from serein.compat.narratives import narrative_transaction, RevisionInbox
from serein.compat.narrative_cleanup import cleanup_retired_bindings
from serein.compat.scout import Scout
from serein.core import Store
from test_live_clients import live
from test_live_narratives import seed


@pytest.mark.parametrize('status', ['archived', 'superseded'])
def test_cleanup_retains_text_dates_history_and_is_idempotent(live, status):
    settings, client = live
    event, scene = seed(client, settings)
    with Store(settings.database) as store, store.transaction():
        store.conn.execute('UPDATE fact_events SET status=? WHERE item_id=?', (status, event))
        store.conn.execute('INSERT INTO fact_event_arc_links VALUES (?,?,?,?)', ('arc:rain', event, 'hash', '2026-01-01'))
        store.conn.execute('INSERT INTO event_arc_links VALUES (?,?,?,?)', ('arc:rain', event, 'hash', '{}'))
    with narrative_transaction(settings.database, write=True) as rolls:
        before = rolls.read('narrative_test')
        removed = cleanup_retired_bindings(rolls)
        after = rolls.read('narrative_test')
        assert removed[0]['removed'] == {'event_ids': [event], 'scene_ids': []}
        assert after['linked_event_ids'] == [] and after['linked_scene_ids'] == [scene]
        for field in ('body', 'full_document', 'document_sha256', 'published_at'):
            assert before[field] == after[field]
        assert after['revision'] == before['revision'] + 1
        assert event in after['history'][-1]['linked_event_ids']
        assert rolls.store.conn.execute('SELECT count(*) FROM fact_events WHERE item_id=?', (event,)).fetchone()[0] == 1
        for table in ('fact_event_arc_links', 'event_arc_links'):
            assert rolls.store.conn.execute(f'SELECT count(*) FROM {table} WHERE event_id=?', (event,)).fetchone()[0] == 0
        assert cleanup_retired_bindings(rolls) == []
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').json()['linked_event_ids'] == []


@pytest.mark.parametrize('decision', ['pending', 'dismissed'])
def test_scheduled_cleanup_does_not_recreate_freshness_hint(live, tmp_path, decision):
    settings, client = live
    event, scene = seed(client, settings)
    config = tmp_path / 'germany.yaml'
    config.write_text('narrative_rolls:\n  new_roll_scout_enabled: false\n', encoding='utf-8')
    settings = replace(settings, background={'germany_config_file': str(config)})
    with Store(settings.database) as store, store.transaction():
        store.conn.execute('UPDATE documents SET updated_at=? WHERE id=?', ('2090-01-01T00:00:00+00:00', scene))
    scout = Scout(settings)
    first = asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert first['stale_roll_hints_created'] == 1
    with narrative_transaction(settings.database, write=True) as rolls:
        inbox = RevisionInbox(rolls.store)
        raw = inbox._load()
        raw['items'][0]['status'] = decision
        old_id = raw['items'][0]['proposal_id']
        inbox._save(raw)
        rolls.store.conn.execute("UPDATE fact_events SET status='archived' WHERE item_id=?", (event,))
    second = asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert second['stale_roll_hints_created'] == 0
    assert len(second['retired_material_bindings_removed']) == 1
    with narrative_transaction(settings.database) as rolls:
        hints = RevisionInbox(rolls.store)._load()['items']
        assert len(hints) == 1 and hints[0]['status'] == decision
        assert old_id in hints[0]['previous_proposal_ids']
    third = asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert third['retired_material_bindings_removed'] == []
    assert third['stale_roll_hints_created'] == 0


def test_archived_scene_removed_but_missing_and_deleted_are_not_silently_removed(live):
    settings, client = live
    event, scene = seed(client, settings)
    with narrative_transaction(settings.database, write=True) as rolls:
        rolls.store.set_lifecycle(scene, 'archived')
        rolls.store.conn.execute("UPDATE fact_events SET status='tombstoned' WHERE item_id=?", (event,))
        result = cleanup_retired_bindings(rolls)
        assert result[0]['removed'] == {'event_ids': [], 'scene_ids': [scene]}
        assert rolls.read('narrative_test')['linked_event_ids'] == [event]


def test_cleanup_alone_does_not_hint_but_later_material_changes_do(live, tmp_path):
    settings, client = live
    event, scene = seed(client, settings)
    config = tmp_path / 'germany.yaml'
    config.write_text('narrative_rolls:\n  new_roll_scout_enabled: false\n', encoding='utf-8')
    settings = replace(settings, background={'germany_config_file': str(config)})
    with Store(settings.database) as store, store.transaction():
        store.conn.execute("UPDATE fact_events SET status='archived' WHERE item_id=?", (event,))
        store.conn.execute('UPDATE documents SET updated_at=? WHERE id=?', ('2000-01-01T00:00:00+00:00', scene))
    scout = Scout(settings)
    cleaned = asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert len(cleaned['retired_material_bindings_removed']) == 1
    assert cleaned['stale_roll_hints_created'] == 0
    with Store(settings.database) as store, store.transaction():
        store.conn.execute('UPDATE documents SET updated_at=? WHERE id=?', ('2090-01-01T00:00:00+00:00', scene))
    fresh = asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert fresh['stale_roll_hints_created'] == 1


def test_cleanup_supports_imported_roll_without_arc_key(live):
    settings, client = live
    event, _ = seed(client, settings)
    with narrative_transaction(settings.database, write=True) as rolls:
        doc = rolls.store.read('narrative_test')
        metadata = doc['metadata']
        del metadata['legacy_registry']['arc_key']
        rolls.store.conn.execute('UPDATE revisions SET metadata_json=? WHERE document_id=? AND number=?',
            (json.dumps(metadata), 'narrative_test', doc['revision']))
        rolls.store.conn.execute("UPDATE fact_events SET status='archived' WHERE item_id=?", (event,))
        assert len(cleanup_retired_bindings(rolls)) == 1
        assert rolls.read('narrative_test')['linked_event_ids'] == []
        assert cleanup_retired_bindings(rolls) == []
