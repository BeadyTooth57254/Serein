import asyncio
import base64
import io
import json
import sqlite3
import tarfile

import pytest
from fastapi.testclient import TestClient
from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.work_tasks import execute,work,status


def archive():
    output=io.BytesIO()
    files={'buckets/dynamic/a.md':'---\nid: a\nname: 约定\n---\n约好周六去书店。',
           'buckets/dynamic/b.md':'---\nid: b\nname: 如约\n---\n周六如约在书店见面。',
           'state/memory_edges.jsonl':json.dumps({'source':'b','target':'a','relation':'updates'})}
    with tarfile.open(fileobj=output,mode='w:gz') as tar:
        for name,content in files.items():
            content=content.encode();info=tarfile.TarInfo(name);info.size=len(content);tar.addfile(info,io.BytesIO(content))
    return base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def api(tmp_path):
    settings=Settings(tmp_path/'serein.db',tmp_path/'index.sqlite',writable=True);initialize(settings)
    # No lifespan worker: tests explicitly run durable queued work without remote services.
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    yield settings,client
    client.close()


def test_web_migration_preview_confirm_queue_resume_and_backup(api,monkeypatch,tmp_path):
    settings,client=api
    assert client.get('/v1/migration',headers={'Authorization':''}).status_code==401
    assert client.get('/v1/export/backup',headers={'Authorization':''}).status_code==401
    preview=client.post('/v1/migration/preview',json={'content':archive()})
    assert preview.status_code==200,preview.text
    item=preview.json();identifier=item['id'];key='legacy:'+identifier
    assert item['summary']['old_edges']==1
    with Store(settings.database,read_only=True) as store:assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0]==0
    options={'user_name':'Mira','ai_name':'Sol','aliases':[],'confirmed':False}
    route='/v1/migration/'+identifier+'/continue'
    assert client.post(route,json=options).status_code==400
    options['confirmed']=True
    assert client.post(route,json=options).status_code==400
    save_settings(settings.database,{'models':[{'id':'test','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'operit_tagging':'test','embedding':'test'}})
    calls=[]
    async def tag(self,item,feedback=''):calls.append(item['old_id'])
    async def refresh(*args, **kwargs):
        assert kwargs['retry_failed'] is True
        return {'cues':{'failed_scenes':[]}}
    monkeypatch.setattr('serein.legacy_migration.workflow.Migration.tag',tag)
    monkeypatch.setattr('serein.configured_models.prepare_selected',lambda *args,**kwargs:{'status':'ready'})
    monkeypatch.setattr('serein.configured_models.effective_settings',lambda s:s)
    monkeypatch.setattr('serein.recall.legacy_indexes.refresh',refresh)
    async def forbidden(*args,**kwargs):raise AssertionError('unexpected model call')
    monkeypatch.setattr('serein.legacy_migration.workflow.complete',forbidden)
    queued=client.post(route,json=options).json()
    asyncio.run(execute(settings.database,key,lambda:work(settings,key,{}),queued_id=queued['run_id']))
    assert status(settings.database,key)['status']=='completed'
    # Refresh/list and explicitly re-queue completed work: no duplicate bodies/edges/tag calls.
    assert client.get('/v1/migration').json()['items'][0]['task']['status']=='completed'
    again=client.post(route,json=options).json()
    asyncio.run(execute(settings.database,key,lambda:work(settings,key,{}),queued_id=again['run_id']))
    assert calls==['a','b']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0]==2
        assert store.conn.execute('SELECT count(*) FROM scene_relations').fetchone()[0]==1
    response=client.get('/v1/export/backup')
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    file=tmp_path/'download.db';file.write_bytes(response.content)
    with sqlite3.connect(file) as conn:
        assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0]==2
    assert client.post(route,json={**options,'ai_name':'different'}).status_code==400


def test_imported_edge_cancel_restore_and_body_change(api):
    settings,client=api
    from serein.legacy_migration.web import upload,read_plan
    from serein.legacy_migration.workflow import Migration
    uploaded=upload(settings,archive());plan=read_plan(settings,uploaded['id'])
    migration=Migration(settings,plan,{'user_name':'Mira','ai_name':'Sol'})
    try:
        migration.import_bodies();asyncio.run(migration.edges())
        edge=client.get('/api/scene-edges').json()['edges'][0]
        assert edge['evidence_origin']=='legacy_graph'
        assert len(client.post('/api/serein/memory-projection',json={}).json()['edges'])==1
        deleted=client.request('DELETE','/api/scene-edges/'+edge['edge_id'],json={'scene_id':edge['source'],'confirm':'DELETE_SCENE_EDGE'})
        assert deleted.status_code==200,deleted.text
        assert client.post('/api/serein/memory-projection',json={}).json()['edges']==[]
        assert client.post('/api/scene-edges/'+edge['edge_id']+'/restore',json={'confirm':'RESTORE_SCENE_EDGE'}).status_code==200
        with Store(settings.database) as store,store.transaction(immediate=True):
            doc=store.read(edge['source']);store.revise(doc['id'],expected_revision=doc['revision'],title=doc['title'],body_md='正文已改变',metadata=doc['metadata'])
        assert client.post('/api/serein/memory-projection',json={}).json()['edges']==[]
    finally:migration.close()


def test_pause_between_items_and_resume_after_page_refresh(api,monkeypatch):
    settings,client=api
    save_settings(settings.database,{'models':[{'id':'test','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'operit_tagging':'test','embedding':'test'}})
    identifier=client.post('/v1/migration/preview',json={'content':archive()}).json()['id'];key='legacy:'+identifier
    options={'user_name':'Mira','ai_name':'Sol','aliases':['旧称'],'confirmed':True}
    route='/v1/migration/'+identifier+'/continue';calls=[]
    async def tag(self,item,feedback=''):
        calls.append(item['old_id'])
        if len(calls)==1:
            from serein.work_tasks import pause
            pause(settings.database,key)
    async def refresh(*args, **kwargs):
        assert kwargs['retry_failed'] is True
        return {'cues':{'failed_scenes':[]}}
    monkeypatch.setattr('serein.legacy_migration.workflow.Migration.tag',tag)
    monkeypatch.setattr('serein.configured_models.prepare_selected',lambda *args,**kwargs:{'status':'ready'})
    monkeypatch.setattr('serein.configured_models.effective_settings',lambda s:s)
    monkeypatch.setattr('serein.recall.legacy_indexes.refresh',refresh)
    queued=client.post(route,json=options).json()
    asyncio.run(execute(settings.database,key,lambda:work(settings,key,{}),queued_id=queued['run_id']))
    item=client.get('/v1/migration').json()['items'][0]
    assert item['task']['status']=='paused' and calls==['a']
    assert item['options']=={**{k:v for k,v in options.items() if k!='confirmed'},'generate_cues':True}
    queued=client.post(route,json={**item['options'],'generate_cues':False,'confirmed':True}).json()
    assert client.get('/v1/migration').json()['items'][0]['options']['generate_cues'] is False
    assert client.post(route,json={**item['options'],'confirmed':True}).status_code==409
    assert client.post(route,json={**item['options'],'generate_cues':'false','confirmed':True}).status_code==422
    asyncio.run(execute(settings.database,key,lambda:work(settings,key,{}),queued_id=queued['run_id']))
    assert status(settings.database,key)['status']=='completed' and calls==['a','b']


def test_parallel_upload_rejected_with_useful_conflict(api):
    settings,client=api
    from serein.file_lock import exclusive_lock
    with exclusive_lock(settings.database.parent/'migrations'/'operation.lock'):
        response=client.post('/v1/migration/preview',json={'content':archive()})
        assert response.status_code==409


def test_path_preview_uses_read_only_server_source_and_rechecks_before_work(api,tmp_path_factory,monkeypatch):
    settings,client=api
    old=tmp_path_factory.mktemp('old-ombre')
    bucket=old/'buckets'/'dynamic';bucket.mkdir(parents=True)
    file=bucket/'one.md';file.write_text('---\nid: old-one\nname: 旧约定\n---\n一起去书店。','utf-8')
    original=file.read_bytes()
    monkeypatch.setenv('SEREIN_MIGRATION_PATH_ROOT',str(old))
    response=client.post('/v1/migration/preview-path',json={'path':str(old),'display_path':'/host/old-ombre'})
    assert response.status_code==200,response.text
    identifier=response.json()['id']
    assert response.json()['summary']['scenes']==1
    assert client.get('/v1/migration').json()['items'][0]['source_path']=='/host/old-ombre'
    assert file.read_bytes()==original
    assert client.post('/v1/migration/preview-path',json={'path':str(settings.database.parent)}).status_code==400
    assert client.post('/v1/migration/preview-path',json={'path':'relative/buckets'}).status_code==400
    file.write_text('---\nid: old-one\nname: 旧约定\n---\n后来改了。','utf-8')
    from serein.legacy_migration.web import run
    with pytest.raises(ValueError,match='预览后发生变化'):run(settings,identifier)
