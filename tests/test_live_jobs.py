import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from serein.compat.germany.scene_linker import SceneLinker
from serein.compat.jobs import update_scene_jobs
from serein.compat.scout import Scout
from serein.compat.narratives import narrative_transaction, RevisionInbox
from serein.compat.scenes import Scenes
from serein.application import Application
from serein.core.store import Store
from test_live_clients import live
from test_live_relations import seed
from test_live_narratives import seed as seed_narrative


def test_scene_job_survives_failure_and_concurrent_edit(live):
    settings, client = live
    source, target, _ = seed(client)
    linker = SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True}})
    linker.handle_scene_content_changed = AsyncMock(return_value={'normal_relink_required':True})
    linker.link_scene = AsyncMock(return_value={'status':'failed'})
    with pytest.raises(RuntimeError):
        asyncio.run(update_scene_jobs(settings,linker))
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM scene_jobs').fetchone()[0] == 2

    async def write_during_model(*args):
        with Store(settings.database) as store:
            store.conn.execute('INSERT INTO scene_jobs(scene_id) VALUES(?)',(source,))
        return {'status':'no_edges'}

    linker.link_scene = AsyncMock(side_effect=write_during_model)
    asyncio.run(update_scene_jobs(settings,linker))
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM scene_jobs').fetchone()[0] == 2
    linker.link_scene = AsyncMock(return_value={'status':'no_edges'})
    asyncio.run(update_scene_jobs(settings,linker))
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM scene_jobs').fetchone()[0] == 0


def test_scout_retains_scan_checkpoint_across_restart(live,tmp_path):
    settings,client=live
    seed(client)
    config=tmp_path/'germany.yaml'
    config.write_text('narrative_rolls:\n  revision_scan_enabled: true\n  new_roll_scout_enabled: false\n','utf-8')
    settings=replace(settings,background={'germany_config_file':str(config)})
    scout=Scout(settings)
    result=asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert result['checked_rolls']==0
    restarted=Scout(settings)
    assert restarted.inbox.scan_metadata()['checked_rolls']==0
    stamp=datetime.fromisoformat(restarted.inbox.scan_metadata()['last_scan_at']).astimezone(ZoneInfo('Asia/Shanghai'))
    assert asyncio.run(restarted.run_due(stamp.replace(hour=23)))['status']=='not_due'


def test_revision_background_starts_without_a_selected_model(live, monkeypatch):
    from serein.application import Application
    from serein import model_jobs
    from serein.compat import jobs as background_jobs

    settings, _ = live
    started = asyncio.Event()
    async def observed_run(self):
        if self.scout is not None:
            started.set()
        await asyncio.Future()

    monkeypatch.setattr(background_jobs.BackgroundJobs, 'run', observed_run)

    async def check():
        task = asyncio.create_task(model_jobs.run(Application(settings)))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


def test_promoted_event_leaves_revision_inbox_and_future_scout_materials(live):
    settings, client = live
    from serein.deployment import save_settings
    save_settings(settings.database, {'features': {'event_to_scene': True}})
    event_id, _ = seed_narrative(client, settings)
    with narrative_transaction(settings.database, write=True) as rolls:
        RevisionInbox(rolls.store)._save({'items': [{
            'proposal_id': 'old-event-hint', 'proposal_kind': 'new_roll_candidate',
            'narrative_id': '', 'status': 'pending', 'source_event_ids': [event_id]}]})
    before = Scout(settings)
    assert any(item['source_type'] == 'event' and item['source_id'] == event_id
               for item in asyncio.run(before._active_narrative_material_inventory()))
    promoted = Application(settings).services.write('promote-event', 'promote_event', {
        'event_id': event_id, 'expected_revision': 1,
        'title': '我记得的雨声', 'body_md': '那天的雨声还在。'})
    assert Scenes(settings.database).evidence(promoted['id'])['evidence_status'] == 'bound'
    with narrative_transaction(settings.database) as rolls:
        assert RevisionInbox(rolls.store).list()['count'] == 0
    scout = Scout(settings)
    inventory = asyncio.run(scout._active_narrative_material_inventory())
    assert not any(item['source_type'] == 'event' and item['source_id'] == event_id for item in inventory)
    assert any(item['source_type'] == 'scene' and item['source_id'] == promoted['id'] for item in inventory)
    narrative = asyncio.run(scout.read_narrative('narrative_test'))
    freshness = asyncio.run(scout._narrative_material_freshness(narrative, ZoneInfo('Asia/Shanghai')))
    assert not any(item['source_type'] == 'event' and item['source_id'] == event_id for item in freshness)
    report = asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert report['checked_rolls'] == 1
