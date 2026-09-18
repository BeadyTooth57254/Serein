import asyncio
import json
from datetime import datetime,timedelta,timezone

import httpx
import pytest
from test_public_features import settings,ingest,output_for,synthetic_runner
from serein.core.store import Store,encode
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.extensions.pipeline_limits import blocks
from serein.imports import stage,advance_import
from serein import work_tasks as work


def pairs(number,known=False):
    stamp=datetime(2025,1,1,tzinfo=timezone.utc)
    result=[]
    for n in range(number):
        for offset,role in enumerate(('user','assistant')):
            result.append({'id':n*2+offset+1,'role':role,'content':'Synthetic dialogue',
                           'created_at':(stamp+timedelta(minutes=n*25+offset)).isoformat(),
                           'metadata':{'timestamp_source':'export' if known else 'import_time'}})
    return result


def test_time_blocks_and_unknown_time_twenty_rounds_preserve_ids():
    known=pairs(4,True)
    assert [len(b) for b in blocks(known)]==[2,2,2,2]
    unknown=pairs(58)
    split=blocks(unknown)
    assert [len(b) for b in split]==[40,40,36]
    assert [m['id'] for b in split for m in b]==list(range(1,117))
    assert all(b[0]['role']=='user' and b[-1]['role']=='assistant' for b in split)
    constrained=blocks(unknown,max_chars=100)
    assert all(sum(len(m['content']) for m in b)<=100 for b in constrained)
    unknown[0]['content']='x'*101
    with pytest.raises(ValueError,match='单轮'):blocks(unknown,max_chars=100)


def test_import_boundary_keeps_concurrent_new_chats_and_retires_mixed_plan(settings,monkeypatch):
    from serein.compat.raw_archive import raw_archive
    from serein.compat.originals import Originals
    data={'id':'imported-history','messages':[{'role':'user','content':'historical-'+'x'*55720},
                                           {'role':'assistant','content':'historical reply'}]}
    data['messages'] += [{'role':'user' if i%2==0 else 'assistant','content':'historical extra '+str(i)} for i in range(24)]
    upload=stage(settings.database,json.dumps(data),'history.json','auto',False)
    assert advance_import(settings,upload['id'])['processed']==25
    # New chats can arrive while an import is in progress; never move a global cursor.
    raw_archive(settings).ingest([{'source_event_id':str(i),'session_id':'new-chat','role':m['role'],
        'text':'New synthetic dialogue '+str(i),'created_at':m['created_at']} for i,m in enumerate(pairs(5,True))],source='test')
    assert advance_import(settings,upload['id'])['status']=='completed'
    p.initialize(settings.database)
    with Store(settings.database) as store:
        imported=[r[0] for r in store.conn.execute("SELECT id FROM raw_events WHERE json_extract(metadata_json,'$.import_upload_id')=?",(upload['id'],))]
        # Simulate a stale pre-upgrade plan which mixed archive-only and new rows.
        all_rows=[p.message(r) for r in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]
        frozen={'contract':p.CONTRACT,'messages':all_rows,'routing_messages':all_rows,'scope':'old'}
        store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json) VALUES (?,?,?)',('old-mixed','old',encode(frozen)))
        store.conn.execute("DELETE FROM raw_processing WHERE outcome='archived_only'")
    p.initialize(settings.database)  # Upgrade recovers historical import membership.
    with Store(settings.database) as store:
        assert store.conn.execute("SELECT status FROM pipeline_batches WHERE id='old-mixed'").fetchone()[0]=='superseded_import_boundary'
        assert {r[0] for r in store.conn.execute('SELECT raw_id FROM raw_processing')}==set(imported)
    batch=p.new_batch(settings.database,True)
    assert batch and all(m['id'] not in imported for m in json.loads(batch['input_json'])['messages'])
    calls=[]
    async def routing(database,batch,data,runner):
        calls.extend(m['id'] for m in data['routing_messages'])
        return await original(database,batch,data,synthetic_runner)
    original=p.route_batch
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
                                    'assignments':{'track_router':'local'}})
    monkeypatch.setattr(p,'route_batch',routing)
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==10 and not set(calls)&set(imported)
    found=Originals(settings.database).source_message_search('historical',limit=50)['items']
    assert len(found)==26
    assert Originals(settings.database).source_message_read([found[-1]['id']])['items'][0]['content'].startswith('historical')


def test_legacy_archive_only_originals_never_enter_daytime_routing(settings,monkeypatch):
    from serein.compat.raw_archive import raw_archive
    raw_archive(settings).ingest([{'source_event_id':str(i),'session_id':'old','role':m['role'],
        'text':m['content'],'created_at':m['created_at']} for i,m in enumerate(pairs(5,True))],source='old-chat')
    p.initialize(settings.database)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO raw_processing SELECT id,'legacy-originals:synthetic','archived_only' FROM raw_events")
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
                                    'assignments':{'track_router':'local'}})
    async def forbidden(*args):pytest.fail('Imported originals reached model routing')
    monkeypatch.setattr(p,'route_batch',forbidden)
    asyncio.run(p.flush_routes(settings.database))
    assert p.new_batch(settings.database,True) is None


def test_116_unknown_time_originals_are_processed_in_small_batches(settings):
    messages=[{'role':m['role'],'content':m['content']} for m in pairs(58)]
    # Seed raw dialogue directly; imported history now remains archive-only.
    from serein.imports import parse_file, ImportArchive
    data=parse_file(json.dumps({'id':'one-session','messages':messages}),'conversation.json',{'user_name':'User','ai_name':'AI'})
    ImportArchive({'raw_events':{'db_path':str(settings.database)}}).ingest(data['entries'])
    tasks=[]
    async def runner(role,request):
        if role=='track_router':tasks.append(len(request['messages']))
        return output_for(role,request)
    # This fixture deliberately skips completed units, keeping the batching test
    # independent of rolling-Event selection and eventual evidence growth.
    async def skip_runner(role,request):
        if role=='event_curator':return {'events':[],'skip_unit_roots':[u['unit_root_message_id'] for u in request['component']['memberships'] if u['unit_root_message_id'] in {m['id'] for m in request['component']['messages']}],'defer_unit_roots':[]}
        return await runner(role,request)
    for _ in range(4):
        result=asyncio.run(p.advance(settings.database,include_recent=True,runner=skip_runner))
        if result['status']=='current':break
    assert tasks==[40,40,36]
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==116
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==116


def test_oversized_pending_batch_reuses_accepted_router_output(settings):
    ingest(settings)
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    request=p.request_for(settings.database,batch,'track_router')
    output=output_for('track_router',request)
    with Store(settings.database) as store:
        # Emulate the old pending batch with >20 rounds and one validated Router job.
        original=json.loads(batch['input_json']);extended=[]
        for n in range(21):
            for m in original['messages']:
                extended.append({**m,'id':m['id']+n*2,'metadata':{'timestamp_source':'import_time'}})
        original.update(messages=extended,routing_messages=extended)
        request['messages']=extended;output=output_for('track_router',request)
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(original),batch['id']))
        store.conn.execute('INSERT INTO pipeline_jobs(id,batch_id,role,request_json,output_json) VALUES (?,?,?,?,?)',
            (batch['id']+':track_router:0',batch['id'],'track_router:0',encode(request),encode(output)))
    new=p.new_batch(settings.database,True)
    assert new['id']!=batch['id']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()[0]=='superseded_input_budget'
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0]==42
        assert store.conn.execute('SELECT output_json FROM pipeline_jobs').fetchone()[0]==encode(output)


def test_interrupted_batch_keeps_its_normalized_routes_after_daytime_flush(settings):
    """A route written after freezing must not be reinterpreted against old cards."""
    ingest(settings)
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json'])
    # The router succeeded before interruption.  Daytime routing then persists
    # its assignment and created Track card, while the frozen batch itself is
    # still in the old on-disk format without a route snapshot.
    routed=asyncio.run(p.route_batch(settings.database,batch,data,synthetic_runner))
    assignments,_,_=p.route_result(data,routed)
    with Store(settings.database) as store:
        from serein.extensions import pipeline_tracks as track_state
        track_state.persist(store.conn,routed['track_state_updates'],data['scope'])
        for assignment in assignments:
            store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)',
                               (assignment['source_message_id'],encode(assignment)))
    calls=[]
    async def runner(role,request):
        calls.append(role)
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['events']==1
    assert calls==['event_curator','event_writer']
    with Store(settings.database,read_only=True) as store:
        saved=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()[0])
        assert saved['routing_result']['_public_normalized'] is True
        assert saved['routing_result']['assignments']==assignments


def test_bad_cached_track_route_is_held_for_repair_without_consuming_originals(settings):
    ingest(settings)
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json'])
    with Store(settings.database) as store:
        for message in data['routing_messages']:
            store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)',(message['id'],encode({
                'source_message_id':message['id'],'primary_track_id':'session_foreign_track_0001',
                'context_track_ids':[],'routing_role':'primary_activity'})))
    calls=[]
    async def runner(role,request):
        calls.append(role)
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['status']=='needs_repair' and 'session_foreign_track_0001' in result['reason']
    assert calls==[]
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()[0]=='needs_repair'


@pytest.mark.parametrize('role',['track_router','event_curator'])
def test_id_correction_retry_keeps_bad_output_and_accepts_only_valid(settings,monkeypatch,role):
    ingest(settings)
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    if role=='event_curator':
        p.submit(settings.database,task['job_id'],output_for('track_router',task['request']))
        task=asyncio.run(p.advance(settings.database,include_recent=True))
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{role:'local'},'pipeline':{'timeout_seconds':600}})
    calls=[]
    async def complete(model,payload):
        calls.append((model,payload));output=output_for(role,task['request'])
        if len(calls)==1:
            if role=='track_router':output['message_assignments'][0]['source_message_id']=999999
            else:output['events'][0]['owned_unit_roots']=[999999]
        return {'choices':[{'message':{'content':json.dumps(output)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert result['status']=='awaiting_agent' and result['role']!=role
    assert len(calls)==2 and calls[0][0]['request_timeout_seconds']==600
    assert 'allowed_ids' in calls[1][1]['messages'][1]['content'] and '999999' in calls[1][1]['messages'][1]['content']
    with Store(settings.database,read_only=True) as store:
        attempts=store.conn.execute('SELECT * FROM pipeline_attempts WHERE job_id=? ORDER BY id',(task['job_id'],)).fetchall()
        assert len(attempts)==2 and attempts[0]['error'] and '999999' in attempts[0]['output_text'] and not attempts[1]['error']
        assert '999999' not in store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE id=?',(task['job_id'],)).fetchone()[0]


def test_timeout_durable_diagnostic_and_prompt_budget_before_model(settings,monkeypatch):
    ingest(settings)
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'track_router':'local'},'pipeline':{'timeout_seconds':900}})
    called=[]
    async def timeout(model,payload):
        called.append(model);raise httpx.ReadTimeout('synthetic')
    monkeypatch.setattr('serein.model_runtime.complete',timeout)
    with pytest.raises(httpx.ReadTimeout):asyncio.run(p.advance(settings.database,include_recent=True))
    assert called[0]['request_timeout_seconds']==900
    state=work.status(settings.database,'pipeline')
    assert state['status']=='failed' and '超时' in state['error']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT output_json FROM pipeline_jobs').fetchone()[0] is None
        assert store.conn.execute('SELECT error FROM pipeline_attempts').fetchone()[0]
    # Programmatic low limit tests the final serialized-request guard.
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':1}})
    with pytest.raises(ValueError,match='提示词'):asyncio.run(p.advance(settings.database,include_recent=True))
    assert len(called)==1


def test_legacy_116_originals_eleven_router_jobs_resume_without_repeating_them(settings):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.compat.raw_archive import raw_archive
    from serein.core.store import digest
    stamp=datetime(2025,1,1,tzinfo=timezone.utc)
    records=[]
    for n in range(116):
        group=min(n//10,10)
        records.append({'source_event_id':str(n),'session_id':'legacy','role':'user' if n%2==0 else 'assistant',
                        'text':f'Synthetic original {n}', 'created_at':(stamp+timedelta(minutes=group*30,seconds=n)).isoformat()})
    raw_archive(settings).ingest(records,source='synthetic')
    p.initialize(settings.database)
    with Store(settings.database) as store:
        messages=[p.message(row) for row in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]
        scope=digest(encode(['synthetic','legacy']))[:20]
        data={'contract':p.CONTRACT,'messages':messages,'parked':[],'routing_messages':messages,'tracks':[],
              'scope':scope,'source':'synthetic','recent':[],'day':'2025-01-01'}
        batch={'id':'pipeline:legacy116','scope':scope,'input_json':encode(data)}
        store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json) VALUES (?,?,?)',tuple(batch.values()))
    routed=asyncio.run(p.route_batch(settings.database,batch,data,synthetic_runner))
    component=p.components(settings.database,data,routed)[0]
    old=p.request_for(settings.database,batch,'event_curator',component=component)
    old['prompt']='Synthetic oversized prompt. '*3500
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_jobs WHERE output_json IS NOT NULL').fetchone()[0]==11
        store.conn.execute('INSERT INTO pipeline_jobs(id,batch_id,role,request_json) VALUES (?,?,?,?)',
            ('legacy-curator',batch['id'],'event_curator:0',encode(old)))
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
                                     'assignments':{'event_curator':'local'}})
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    from serein import work_tasks
    async def quiet_tick():return {'status':'settled_today'}
    asyncio.run(work_tasks.execute(settings.database,'pipeline',quiet_tick))
    assert work_tasks.status(settings.database,'pipeline')['status']=='idle'
    state=client.get('/v1/pipeline/status').json()
    assert state['status']=='interrupted' and state['stage']=='event_curator' and state['completed']==11
    assert state['attempts']==[]
    with Store(settings.database) as store:
        store.conn.execute("UPDATE background_state SET value_json=json_set(value_json,'$.status','completed','$.stage','settled_today') WHERE name='work:pipeline'")
    restored=client.get('/v1/pipeline/status').json()
    assert restored['status']=='interrupted' and restored['completed']==11
    save_settings(settings.database,{'assignments':{'event_curator':''}})
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert task['role']=='event_curator' and len(task['request']['component']['messages'])==10
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==116
        assert store.conn.execute('SELECT count(*) FROM pipeline_jobs WHERE output_json IS NOT NULL').fetchone()[0]==11
        assert store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE id=?',('legacy-curator',)).fetchone()[0] is None


def test_auto_pause_does_not_route_originals_or_replace_progress(settings,monkeypatch):
    save_settings(settings.database,{'pipeline':{'auto_enabled':False}})
    def unexpected(*args,**kwargs):raise AssertionError('Paused scheduler must not route or call models')
    monkeypatch.setattr(p,'flush_routes',unexpected)
    with Store(settings.database,read_only=True) as store:before='\n'.join(store.conn.iterdump())
    assert asyncio.run(p.scheduled_advance(settings.database))=={'status':'auto_paused'}
    with Store(settings.database,read_only=True) as store:assert '\n'.join(store.conn.iterdump())==before


def test_enabling_auto_pipeline_starts_after_latest_original_and_reenable_moves_boundary(settings):
    p.initialize(settings.database)
    save_settings(settings.database,{'pipeline':{'auto_enabled':False}})
    ingest(settings,1)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json) VALUES ('old-pending','scope','{}')")
    save_settings(settings.database,{'pipeline':{'auto_enabled':True}})
    with Store(settings.database,read_only=True) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT raw_id,outcome FROM raw_processing ORDER BY raw_id')]==[(1,'auto_boundary'),(2,'auto_boundary')]
        assert store.conn.execute("SELECT status FROM pipeline_batches WHERE id='old-pending'").fetchone()[0]=='superseded_auto_boundary'
        boundary=json.loads(store.conn.execute("SELECT value_json FROM background_state WHERE name='pipeline_auto_boundary'").fetchone()[0])
        assert boundary['raw_id']==2 and boundary['skipped_originals']==2
    ingest(settings,2)
    save_settings(settings.database,{'resume':{'recent_original_limit':12}})
    with Store(settings.database,read_only=True) as store:
        assert [row[0] for row in store.conn.execute('SELECT id FROM raw_events WHERE NOT EXISTS (SELECT 1 FROM raw_processing p WHERE p.raw_id=raw_events.id) ORDER BY id')]==[3,4]
    save_settings(settings.database,{'pipeline':{'auto_enabled':False}})
    ingest(settings,3)
    save_settings(settings.database,{'pipeline':{'auto_enabled':True}})
    ingest(settings,4)
    with Store(settings.database,read_only=True) as store:
        assert [row[0] for row in store.conn.execute("SELECT raw_id FROM raw_processing WHERE outcome='auto_boundary' ORDER BY raw_id")]==[1,2,3,4,5,6]
        assert [row[0] for row in store.conn.execute('SELECT id FROM raw_events WHERE NOT EXISTS (SELECT 1 FROM raw_processing p WHERE p.raw_id=raw_events.id) ORDER BY id')]==[7,8]
