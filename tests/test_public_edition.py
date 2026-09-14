import asyncio
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from serein.application import Application
from serein.deployment import save_settings
from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.bootstrap import initialize
from serein.config import Settings, load_settings
from serein.core.store import Store, Conflict
from serein.core.personal import Personal
from serein.extensions.event_pipeline import process_pending
from serein.compat.raw_archive import raw_archive


@pytest.fixture
def settings(tmp_path):
    value=Settings(tmp_path/'serein.db',tmp_path/'index.sqlite',writable=True,
                   extensions={'handoff':{'enabled':True,'page_chars':1000}})
    initialize(value)
    save_settings(value.database,{'features':{'resume':True}})
    return value


def favorite(settings,key,body):
    with Store(settings.database) as store:
        store.create(key,'scene',key,body)
    Personal(settings.database).save('favorite',key,{'favorite':True},document_id=key)


def test_resume_every_new_window_returns_all_explicit_current_bodies(settings):
    favorite(settings,'scene_b','B'*600)
    favorite(settings,'scene_a','A'*1600)
    app=Application(settings)
    resume=app.contributions.tools['resume']
    first=resume('window-1')
    assert first['favorite_ids']==['scene_a'] and first['total_favorites']==2
    assert first['has_more'] and not first['injected']
    assert first['items'][0]['body_md']=='A'*1000
    second=resume('window-1',first['next_cursor'])
    third=resume('window-1',second['next_cursor'])
    assert not third['has_more']
    assert ''.join(i['body_md'] for page in [first,second,third] for i in page['items'])=='A'*1600+'B'*600
    assert resume('window-2')['collection_id']==first['collection_id']
    assert resume('window-2')['items']==first['items']


def test_resume_revokes_deleted_favorite_and_rejects_stale_cursor(settings):
    favorite(settings,'scene_a','A'*1500)
    resume=Application(settings).contributions.tools['resume']
    first=resume('test')
    with Store(settings.database) as store:
        store.conn.execute("UPDATE documents SET lifecycle='deleted' WHERE id='scene_a'")
    with pytest.raises((ValueError,Conflict)):
        resume('test',first['next_cursor'])
    assert resume('test')['items']==[]


def test_resume_event_and_favorite_scene_keep_own_ids_for_evidence_read(settings):
    favorite(settings, 'scene-bound', 'Scene body')
    with Store(settings.database) as store:
        store.create('event-bound', 'event', 'Event', 'Event body')
        source = store.add_source('old-upstream', 'Bound original', metadata={'message_id':'old-123'})
        for key in ('scene-bound', 'event-bound'):
            store.bind(key, source)
    app = Application(settings)
    result = app.contributions.tools['resume']('new-window')
    items = {item['id']:item for item in result['items']}
    assert {'scene-bound','event-bound'} <= items.keys()
    for key in ('scene-bound','event-bound'):
        assert 'source_refs' not in items[key]
        assert 'old-123' not in json.dumps(items[key])
        expanded = app.services.read(key, with_evidence=True)
        assert expanded['evidence'][0]['content'] == 'Bound original'


def test_handoff_conflict_and_opt_in_mcp_registration(settings):
    app=Application(settings)
    fn=app.contributions.tools['handoff']
    assert fn('topic','Continue the book discussion')['revision']==1
    with pytest.raises(Conflict):
        fn('topic','A different note')
    assert app.contributions.tools['resume']('new',handoff_key='topic')['handoff']['body']=='Continue the book discussion'
    selected=replace(settings,mcp_tools=['resume','read_memory'])
    server=create_server(Application(selected))
    # Old whitelists may name resume, but it is no longer exposed.
    assert {t.name for t in asyncio.run(server.list_tools())}=={'read_memory'}
    save_settings(settings.database,{'features':{'resume':False}})
    disabled=replace(settings,extensions={})
    assert 'resume' not in {t.name for t in asyncio.run(create_server(Application(disabled)).list_tools())}


def test_standalone_http_scene_evidence_favorite_and_resume(settings):
    with TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'}) as client:
        raw=client.post('/api/ingest-raw',json={'source':'test-chat','session_id':'test-session','events':[
            {'source_event_id':'message-1','role':'user','text':'读书会周六开始。','created_at':'2025-01-01T00:00:00Z'}]}).json()
        key=raw['items'][0]['id']
        source=Application(settings).services.source.read([key])[0]
        saved=Application(settings).services.write('scene-test','save',{'kind':'scene','title':'读书会',
            'body_md':'约好了周六的读书会。','source_message_ids':[key]})
        identifier=saved['id']
        read=client.get('/v1/memories/'+identifier).json()
        assert read['evidence'][0]['content']==source['content']
        Personal(settings.database).save('favorite',identifier,{'favorite':True},document_id=identifier)
        resumed=client.post('/v1/extensions/resume',json={'window_id':'new-window'}).json()
        assert resumed['favorite_ids']==[identifier]
        assert resumed['items'][0]['body_md']==read['document']['body_md']
        assert client.post('/v1/host/deliveries',json={'receipt_id':'r1','window_id':'new-window','delivered_ids':[identifier]}).status_code==200
        assert len(client.get('/v1/host/deliveries').json()['items'])==1
        client.headers.pop('Authorization')
        assert client.post('/v1/extensions/resume',json={'window_id':'bad'}).status_code==401


def test_disabled_http_extension_unavailable(settings):
    save_settings(settings.database,{'features':{'resume':False}})
    settings=replace(settings,extensions={})
    with TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'}) as client:
        assert client.post('/v1/extensions/resume',json={'window_id':'x'}).status_code==404


def test_event_pipeline_failure_retry_and_exact_evidence(settings):
    archive=raw_archive(settings)
    raw=archive.ingest([{'source_event_id':'original-1','session_id':'chat-1','role':'user',
        'text':'周六去读书会','created_at':'2025-01-01T00:00:00Z'},
        {'source_event_id':'original-2','session_id':'chat-1','role':'assistant','text':'好，带上那本书。','created_at':'2025-01-01T00:01:00Z'}],source='test')
    identifier=raw['items'][0]['id']
    def fail(request):
        raise RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError):
        process_pending(settings,[],runner=fail)
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM raw_processing').fetchone()[0]==0
    def good(request):
        from test_public_features import output_for
        return output_for(request['role'],request)
    assert process_pending(settings,[],runner=good)['events']==1
    assert process_pending(settings,[],runner=fail)['status']=='current'
    with Store(settings.database) as store:
        key=store.conn.execute("SELECT id FROM documents WHERE kind='event'").fetchone()[0]
        assert store.conn.execute('SELECT COUNT(*) FROM index_outbox').fetchone()[0]>0
    evidence=Application(settings).services.read(key)['evidence']
    assert evidence[0]['content']=='周六去读书会'


def test_semantic_configuration_cannot_silently_change_strategy(tmp_path):
    path=tmp_path/'config.toml'
    path.write_text('[storage]\ndatabase="test.db"\n[embedding]\nendpoint="https://embedding.example/v1/embeddings"\napi_key_env="TEST_KEY"\n')
    with pytest.raises(ValueError,match='routing_file'):
        load_settings(path)


def test_disabled_background_features_do_not_create_clients(settings,monkeypatch):
    import serein.compat.jobs
    monkeypatch.setattr(serein.compat.jobs,'BackgroundJobs',lambda *_args,**_kwargs:pytest.fail('Background client instantiated'))
    app=Application(replace(settings,extensions={'dreams':{'enabled':False},'narrative_scout':{'enabled':False},'relations':{'enabled':False}}))
    assert app.capabilities()['jobs']==['index']


def test_source_paging_context_and_host_delivery_projection(settings):
    archive=raw_archive(settings)
    archive.ingest([{'source_event_id':f'm-{i}','role':'user','text':f'Page {i}',
        'session_id':'alpha' if i!=3 else 'beta'} for i in range(6)],source='test')
    with TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'}) as client:
        first=client.post('/v1/host/messages/search',json={'limit':2}).json()
        second=client.post('/v1/host/messages/search',json={'limit':2,'beforeId':first['next_before_id']}).json()
        assert first['has_more']
        assert {r['id'] for r in first['items']}.isdisjoint({r['id'] for r in second['items']})
        context=client.post('/v1/host/messages/search',json={'contextMessageId':1}).json()
        assert context['mode']=='context' and all(r['session_id']=='alpha' for r in context['items'])
        client.post('/v1/host/deliveries',json={'receipt_id':'delivery-test','window_id':'new','delivered_ids':['scene_test']})
        row=client.get('/v1/host/deliveries').json()['items'][0]
        assert row['gateway_memory_injected_ids']==['scene_test'] and row['hook_memory_outcome']=='injected'


def test_shared_lifecycle_starts_and_cancels_enabled_job(settings):
    from serein.lifecycle import lifespan
    from serein.extensions import Contributions
    observed=[]
    async def job():
        observed.append('started')
        try:
            await asyncio.Future()
        finally:
            observed.append('stopped')
    app=Application(replace(settings,index=None,extensions={'test':{'enabled':True}}),
        extension_factories={'test':lambda *_:Contributions(jobs={'test':job})})
    async def run():
        async with lifespan(app):
            await asyncio.sleep(0)
            assert observed==['started']
    asyncio.run(run())
    assert observed==['started','stopped']
