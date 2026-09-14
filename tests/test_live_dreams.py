import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from serein.compat.dreams import Dreams
from serein.compat.background import DreamMaterialReader
from serein.core.store import Store
from test_live_clients import live


def test_dream_uses_sqlite_and_surfaces_once_without_files(live, monkeypatch):
    settings,client=live
    monkeypatch.delenv('SEREIN_DREAM_API_KEY',raising=False)
    engine=Dreams(settings)
    stamp=(datetime.now(timezone.utc)-timedelta(hours=5)).isoformat()
    record=engine._write_record({'dream_id':'dream_test','generated_at':stamp,'local_date':'2026-09-07',
        'surfaced':False,'source_bucket_ids':['scene_test'],'core_affect':{'valence':.5,'arousal':.3},'recall_cues':['窗边的雨声']},'窗边的雨声落在掌心。')
    engine._log_event('generated',{'dream_id':record.dream_id,'generated_at':stamp,'local_date':'2026-09-07'})
    assert engine._daily_decision_for_date('2026-09-07')=='generated'
    engine.spontaneous_surface_prob=0
    result=asyncio.run(engine.surface_with_status(valence=.5,arousal=.3))
    assert result['dream_id']=='dream_test'
    assert engine.claim_surface(record,True)['reason']=='already_claimed'
    assert client.get('/api/dreams/dream_test').json()['status']=='surfaced'
    with Store(settings.database) as store:
        assert store.conn.execute("SELECT COUNT(*) FROM historical_work_events WHERE event='surfaced'").fetchone()[0]==1


def test_generated_dream_survives_engine_restart(live, monkeypatch):
    settings,_=live
    monkeypatch.delenv('SEREIN_DREAM_API_KEY',raising=False)
    engine=Dreams(settings)
    engine.client=object()
    materials=[{'id':'scene_test','content':'这是一条测试记忆。','metadata':{'created':datetime.now(timezone.utc).isoformat()}}]*5
    engine.select_materials=AsyncMock(return_value=materials)
    engine._call_dream_model=AsyncMock(return_value='我在雨里醒来。')
    reader=type('Reader',(),{'recent':AsyncMock(return_value=materials)})()
    result=asyncio.run(engine.generate(reader,force=True))
    assert result['status']=='created'
    restarted=Dreams(settings)
    assert restarted.list_records()[0].body=='我在雨里醒来。'
    assert restarted._daily_decision_for_date(result['date'])=='generated'


def test_dream_reads_new_events_and_scenes_then_diary_only_if_empty(live):
    settings,client=live
    now=datetime.now(timezone.utc)
    old=(now-timedelta(days=5)).isoformat(timespec='seconds')
    with Store(settings.database) as store:
        store.create('event_old','event','旧事','旧正文',created_at=old,updated_at=now.isoformat())
        store.create('scene_new','scene','新 Scene','窗边的雨',created_at=now.isoformat())
        store.create('event_new','event','新 Event','一只白猫',created_at=now.isoformat())
        store.create('scene_archived','scene','归档','不应入梦',lifecycle='archived',created_at=now.isoformat())
    diary=client.post('/diaries',json={'content':'日记里的船','author':'user'}).json()
    assert diary['id']
    reader=DreamMaterialReader(settings.database)
    materials=asyncio.run(reader.recent(now+timedelta(seconds=1)))
    assert {m['id'] for m in materials}=={'event_new','scene_new'}
    with Store(settings.database) as store:
        store.conn.execute("UPDATE documents SET created_at=? WHERE id IN ('event_new','scene_new')",(old,))
    fallback=asyncio.run(reader.recent(datetime.now(timezone.utc)+timedelta(seconds=1)))
    assert len(fallback)==1 and fallback[0]['object_kind']=='diary'


def test_dream_is_after_four_and_probability_is_saved(live,monkeypatch):
    settings,client=live
    engine=Dreams(settings)
    engine.client=object()
    reader=type('Reader',(),{'recent':AsyncMock(return_value=[{'id':'event_a','content':'灯','object_kind':'event','metadata':{}}])})()
    early=datetime(2026,9,13,3,59,tzinfo=engine.tz)
    assert asyncio.run(engine.run_due(reader,now=early))['reason']=='too_early'
    saved=client.patch('/v1/settings',json={'dream':{'daily_probability':0}})
    assert saved.status_code==200,saved.text
    monkeypatch.setattr('serein.compat.germany.dream_engine.random.random',lambda:0)
    result=asyncio.run(engine.run_due(reader,now=early+timedelta(minutes=2)))
    assert result['reason']=='daily_probability_miss'
    assert engine.daily_hour==4 and engine.daily_probability==0


def test_previewed_legacy_path_can_be_exported_as_zip(live,tmp_path):
    settings,client=live
    source=tmp_path.parent/(tmp_path.name+'-ombre-old')
    source.mkdir()
    (source/'memory.md').write_text('旧记忆正文','utf-8')
    identifier='a'*64
    plan=settings.database.parent/'migrations'/'uploads'/identifier
    plan.mkdir(parents=True)
    (plan/'plan.json').write_text('{}','utf-8')
    (plan/'source.json').write_text(json.dumps({'path':str(source),'display_path':str(source)}),'utf-8')
    response=client.get(f'/v1/migration/{identifier}/export')
    assert response.status_code==200,response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read('memory.md').decode()=='旧记忆正文'
