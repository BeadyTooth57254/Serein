import asyncio
import pytest
from fastapi.testclient import TestClient
from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.application import Application
from serein.compat.window_shadows import WindowShadows
from serein.compat.raw_archive import raw_archive
from serein.core.personal import Personal
from serein.core.store import Store, Conflict
from serein.deployment import save_settings, read_settings
from test_public_features import settings


def test_resume_http_toggle_and_persistent_selection_without_mcp_tool(settings):
    server=create_server(Application(settings))
    names=lambda:{tool.name for tool in asyncio.run(server.list_tools())}
    with TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'}) as client:
        assert 'resume' not in names()
        assert 'source_message_read' not in names()
        assert client.post('/v1/extensions/resume',json={'window_id':'new'}).status_code==404
        save_settings(settings.database,{'features':{'resume':True},'resume':{'pending_originals':False}})
        assert 'resume' not in names()
        with pytest.raises(Exception, match='Unknown tool'):
            asyncio.run(server.call_tool('resume', {'window_id': 'new'}))
        assert client.post('/v1/extensions/resume',json={'window_id':'new'}).json()['selection']['pending_originals'] is False
        assert Application(settings).contributions.tools['resume']('new')['selection']['pending_originals'] is False
        save_settings(settings.database,{'features':{'resume':False}})
        assert 'resume' not in names()
        assert client.post('/v1/extensions/resume',json={'window_id':'new'}).status_code==404


def test_latest_shadow_introductions_manual_edit_retry_and_older_revision(settings):
    save_settings(settings.database,{'features':{'window_shadows':True,'resume':True}})
    shadows=WindowShadows(settings.database)
    old=dict(window_id='a',title='Earlier',user_view='Earlier view',self_view='Earlier self',recent_events='Earlier events')
    current=dict(window_id='b',title='Latest',user_view='Current view',self_view='Current self',recent_events='Current events')
    shadows.write(**old);shadows.write(**current)
    assert shadows.read()['window_id']=='b'
    assert read_settings(settings.database)['identity']['user_description']=='Current view'
    save_settings(settings.database,{'identity':{'user_description':'Human edit'}})
    assert shadows.write(**current)['status']=='unchanged'
    assert read_settings(settings.database)['identity']['user_description']=='Human edit'
    shadows.write(**{**old,'user_view':'Older revised'},expected_revision=1)
    assert read_settings(settings.database)['identity']['user_description']=='Human edit'
    result=Application(settings).contributions.tools['resume']('new')
    assert [item['id'] for item in result['items'] if item['kind']=='shadow']==['shadow:b']
    assert 'Current events' in result['items'][0]['body_md']
    with Store(settings.database) as store:store.record_deletion('shadow:b','2026-09-10',{})
    assert shadows.read()['window_id']=='a'
    with pytest.raises(Conflict):shadows.write(**current)


def test_selected_events_scenes_original_ids_and_pending_toggle(settings):
    save_settings(settings.database,{'features':{'resume':True,'originals':True},'resume':{'selected_memories':True,'selected_ids':['event']}})
    with Store(settings.database) as store:
        store.create('scene','scene','Scene','Selected scene')
        store.create('event','event','Event','Selected event')
        source=store.add_source('session-a:7','Actual original',metadata={'message_id':'7','session_id':'session-a'})
        store.bind('event',source)
    for key in ('scene','event'):Personal(settings.database).save('favorite',key,{'favorite':True},document_id=key)
    raw_archive(settings).ingest([{'source_event_id':'7','session_id':'session-b','role':'user','text':'Different original'}],source='synthetic')
    tools=Application(settings).contributions.tools
    result=tools['resume']('new')
    event=next(item for item in result['items'] if item['id']=='event')
    assert sum(item['id']=='event' for item in result['items'])==1
    assert 'source_refs' not in event
    assert Application(settings).services.read(event['id'],with_evidence=True)['evidence'][0]['content']=='Actual original'
    originals=tools['source_message_read'](['source:'+source,result['items'][-1]['id']])
    assert [item['content'] for item in originals['items']]==['Actual original','Different original']
    assert tools['source_message_read'](['raw:9999'])['missing_ids']==['raw:9999']
    save_settings(settings.database,{'resume':{'favorite_scenes':False,'recent_events':False,'pending_originals':False}})
    assert [item['id'] for item in tools['resume']('new')['items']]==['event']
    with Store(settings.database) as store:store.set_lifecycle('event','deleted')
    assert tools['resume']('new')['items']==[]
    assert tools['source_message_read'](['source:'+source])['missing_ids']==['source:'+source]


def test_picker_filters_pagination_and_explicit_selection_survives_reload(settings):
    with Store(settings.database) as store:
        for index in range(32):
            store.create(f'pick-{index:02}','event','Selected topic',f'Body {index}',created_at='2025-01-02T00:00:00Z')
        store.create('pick-scene','scene','Scene title','Distinct prose',created_at='2025-01-03T00:00:00Z')
        store.create('archived','event','Selected topic','Archived',lifecycle='archived')
    with TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'}) as client:
        first=client.get('/v1/settings/resume-candidates?kind=event&q=topic&date=2025-01-02').json()
        assert first['total']==32 and len(first['items'])==30 and first['has_more']
        second=client.get('/v1/settings/resume-candidates?kind=event&offset=30').json()
        assert len(second['items'])==2 and not second['has_more']
        assert not ({item['id'] for item in first['items']}&{item['id'] for item in second['items']})
        assert client.get('/v1/settings/resume-candidates?kind=scene&q=Distinct').json()['items'][0]['id']=='pick-scene'
        assert client.get('/v1/settings/resume-candidates?kind=&ids=pick-00&ids=pick-scene').json()['total']==2
        changes={'features':{'resume':True},'resume':{'selected_memories':True,'selected_ids':['pick-scene','pick-00','pick-00'],
            'recent_events':False,'favorite_scenes':False,'pending_originals':False,'latest_shadow':False}}
        saved=client.patch('/v1/settings',json=changes)
        assert saved.status_code==200,saved.text
        assert saved.json()['resume']['selected_ids']==['pick-scene','pick-00']
        assert client.patch('/v1/settings',json={'resume':{'selected_ids':['x']*201}}).status_code==422
    resume=Application(settings).contributions.tools['resume']
    assert [item['id'] for item in resume('new')['items']]==['pick-scene','pick-00']
    with Store(settings.database) as store:store.set_lifecycle('pick-00','archived')
    assert [item['id'] for item in resume('new')['items']]==['pick-scene']
    save_settings(settings.database,{'resume':{'selected_memories':False}})
    assert resume('new')['items']==[]
    assert read_settings(settings.database)['resume']['selected_ids']==['pick-scene','pick-00']


def test_resume_can_include_a_bounded_number_of_recent_originals(settings):
    entries=[{'source_event_id':str(index),'session_id':'recent-chat','role':'user' if index%2 else 'assistant',
              'text':f'Original {index}','created_at':f'2026-09-15T00:0{index}:00Z'} for index in range(1,6)]
    raw_archive(settings).ingest(entries,source='synthetic')
    save_settings(settings.database,{'features':{'resume':True},'resume':{
        'latest_shadow':False,'recent_events':False,'favorite_scenes':False,'selected_memories':False,
        'pending_originals':False,'recent_originals':True,'recent_original_limit':3}})
    resume=Application(settings).contributions.tools['resume']
    result=resume('new')
    originals=[item for item in result['items'] if item['section']=='recent_original']
    assert [item['body_md'] for item in originals]==['Original 3','Original 4','Original 5']
    assert result['total_recent_originals']==3
    assert result['raw_message_ids']==[item['raw_id'] for item in originals]
    scoped=resume('new',source_session_id='missing-chat')
    assert scoped['items']==[] and scoped['total_recent_originals']==0
    save_settings(settings.database,{'resume':{'pending_originals':True}})
    pending=resume('new')
    assert pending['selection']['pending_originals'] is True
    assert pending['selection']['recent_originals'] is False
    assert len([item for item in pending['items'] if item['kind']=='raw'])==5
