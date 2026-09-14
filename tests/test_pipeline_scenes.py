import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from test_public_features import settings, ingest, output_for
from serein.api.http import create_app
from serein.core.store import Store, encode, digest
from serein.core.source_scenes import find_source_scenes
from serein.extensions import pipeline as p, pipeline_scenes as scenes


def bind_scene(settings, ids=(1,2), name='scene', lifecycle='active'):
    with Store(settings.database) as store, store.transaction(immediate=True):
        store.create(name,'scene','Earlier reading','The earlier book club plan was recorded.',lifecycle=lifecycle)
        for identifier in ids:
            m=store.conn.execute('SELECT * FROM raw_events WHERE id=?',(identifier,)).fetchone()
            source=store.add_source(encode([m['source'],m['session_id'],m['source_event_id']]),m['text'])
            store.bind(name,source)


def messages(settings):
    with Store(settings.database,read_only=True) as store:
        return [p.message(row) for row in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]


def test_exact_hash_bound_query_deduplicates_and_preserves_archived(settings):
    for i in range(1,8):ingest(settings,i)
    bind_scene(settings,range(1,14))
    keys=scenes.scene_keys(messages(settings)[:13])
    result=find_source_scenes(settings.database,keys)
    assert result['count']==1 and len(result['items'][0]['matched_sources'])==13
    assert result['items'][0]['body']=='The earlier book club plan was recorded.'
    assert find_source_scenes(settings.database,[{**keys[0],'content_sha256':'0'*64}])['count']==0
    assert find_source_scenes(settings.database,[{**keys[0],'session_id':'another'}])['count']==0
    with Store(settings.database) as store:store.set_lifecycle('scene','archived')
    assert find_source_scenes(settings.database,keys)['items'][0]['status']=='archived'
    with Store(settings.database) as store:store.conn.execute("UPDATE evidence_bindings SET active=0 WHERE document_id='scene'")
    assert find_source_scenes(settings.database,keys)['count']==0


def test_query_auth_limits_conflicts_and_deleted_scene(settings):
    ingest(settings);bind_scene(settings,lifecycle='deleted')
    keys=scenes.scene_keys(messages(settings))
    client=TestClient(create_app(settings,token='test',live=True))
    url='/api/scenes/find-by-source-keys'
    assert client.post(url,json={'source_keys':keys}).status_code==401
    response=client.post(url,headers={'Authorization':'Bearer test'},json={'source_keys':keys})
    assert response.status_code==200 and response.json()['count']==0
    for invalid in (None,[],keys*251,[keys[0],{**keys[0],'content_sha256':'0'*64}]):
        with pytest.raises(ValueError):find_source_scenes(settings.database,invalid)
    bind_scene(settings,name='large')
    with Store(settings.database) as store:
        store.revise('large',expected_revision=1,title='Large',body_md='x'*100001)
    with pytest.raises(ValueError,match='budget'):find_source_scenes(settings.database,keys)


def test_pipeline_shared_snapshot_skip_old_keep_new_and_receipts(settings):
    ingest(settings);ingest(settings,2);bind_scene(settings)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:05:00Z' WHERE id IN (3,4)")
    seen=[]
    async def runner(role,request):
        output=output_for(role,request)
        if role=='event_curator':
            snapshot=request['component']['existing_scenes'];seen.append(snapshot)
            assert len(snapshot)==1 and snapshot[0]['matched_source_message_ids']==[1,2]
            assert p.latest.event_curator_model_input(request['component'])['existing_scenes']==snapshot
            output['skip_unit_roots']=[1];output['events'][0]['owned_unit_roots']=[3]
        if role=='event_writer':
            assert request['component']['existing_scenes']==seen[0]
            assert encode(seen[0]) in request['prompt']
            assert [m['id'] for m in request['messages']]==[3,4]
        return output
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['events']==1 and result['skipped']==2 and result['processed_originals']==4
    with Store(settings.database,read_only=True) as store:
        detail=json.loads(store.conn.execute('SELECT details_json FROM pipeline_event_details').fetchone()[0])
        assert detail['existing_scene_receipts'][0]['scene_id']=='scene'
        assert store.read('scene')['revision']==1


@pytest.mark.parametrize('change',['edit','unbind','new_scene'])
def test_changed_scene_rolls_back_event_and_processing_then_restarts(settings,change):
    ingest(settings);bind_scene(settings)
    async def runner(role,request):
        if role=='event_writer':
            if change=='new_scene':bind_scene(settings,name='another')
            else:
                with Store(settings.database) as store:
                    if change=='edit':store.revise('scene',expected_revision=1,title='Edited',body_md='Updated reading experience')
                    else:store.conn.execute("UPDATE evidence_bindings SET active=0 WHERE document_id='scene'")
        return output_for(role,request)
    with pytest.raises(scenes.SceneContextChanged):
        asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        assert store.conn.execute('SELECT status FROM pipeline_batches').fetchone()[0]=='superseded_scene_context'
    calls=[]
    async def retry(role,request):calls.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=retry))['events']==1
    assert calls==list(p.ROLES)


def test_skip_only_still_verifies_scene_before_marking_originals(settings):
    ingest(settings);bind_scene(settings)
    async def runner(role,request):
        if role=='event_curator':
            with Store(settings.database) as store:store.set_lifecycle('scene','deleted')
            return {'events':[],'skip_unit_roots':[1],'defer_unit_roots':[]}
        return output_for(role,request)
    with pytest.raises(scenes.SceneContextChanged):asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_agent_resume_uses_snapshot_and_retires_stale_jobs(settings):
    ingest(settings);bind_scene(settings)
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert task['role']=='event_curator'
    repeated=asyncio.run(p.advance(settings.database,include_recent=True))
    assert repeated['job_id']==task['job_id'] and repeated['request']==task['request']
    with Store(settings.database) as store:store.revise('scene',expected_revision=1,title='Changed',body_md='New body')
    with pytest.raises(scenes.SceneContextChanged):asyncio.run(p.advance(settings.database,include_recent=True))
    with pytest.raises(ValueError,match='任务输入已更新'):p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
    next_task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert next_task['job_id']!=task['job_id']


def test_lookup_failure_never_becomes_empty_context(settings,monkeypatch):
    ingest(settings)
    def fail(*args,**kwargs):raise RuntimeError('Synthetic unavailable lookup')
    monkeypatch.setattr(scenes,'read_bound_scenes',fail)
    called=[]
    async def runner(role,request):called.append(role);return output_for(role,request)
    with pytest.raises(RuntimeError,match='unavailable'):asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert called==['track_router']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_mixed_unit_retains_all_originals(settings):
    ingest(settings);bind_scene(settings,ids=(1,))
    async def runner(role,request):
        if role=='event_writer':assert [m['id'] for m in request['messages']]==[1,2]
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))['events']==1


def test_internal_lookup_deduplicates_across_sql_chunks(settings):
    ingest(settings);bind_scene(settings)
    originals=messages(settings)
    extra=[{**originals[0],'id':i+10,'source_event_id':str(i),'content':'Synthetic unbound'} for i in range(500)]
    result=scenes.read_bound_scenes(settings.database,[*originals,*extra])
    assert len(result)==1 and result[0]['matched_source_message_ids']==[1,2]
