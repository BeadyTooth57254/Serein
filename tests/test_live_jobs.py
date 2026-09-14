import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from serein.compat.germany.scene_linker import SceneLinker
from serein.compat.jobs import update_scene_jobs
from serein.compat.scout import Scout
from serein.core.store import Store
from test_live_clients import live
from test_live_relations import seed


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
    assert result['active_materials_searched']==2
    restarted=Scout(settings)
    assert restarted.inbox.scan_metadata()['active_materials_searched']==2
    stamp=datetime.fromisoformat(restarted.inbox.scan_metadata()['last_scan_at']).astimezone(ZoneInfo('Asia/Shanghai'))
    assert asyncio.run(restarted.run_due(stamp.replace(hour=23)))['status']=='not_due'
