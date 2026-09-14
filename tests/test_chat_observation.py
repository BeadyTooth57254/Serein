import json
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from serein.api.http import create_app
from serein.chat_state import recent_deliveries
from serein.core.store import Store, encode
from test_public_settings import deployment, configure


def history(client):
    response=client.get('/api/gateway-injections')
    assert response.status_code==200 and response.json()['status']=='ok'
    return response.json()['items']


def answer():
    return {'choices':[{'message':{'role':'assistant','content':'Synthetic answer'}}]}


def test_disabled_recall_is_visible_after_reply_and_restart(deployment,monkeypatch):
    settings,client=deployment;configure(client)
    with Store(settings.database) as store:
        store.conn.execute('INSERT INTO injection_debug(session_id,round_id,created_at,payload_json) VALUES (?,?,?,?)',
                           ('legacy',1,'2020-01-01',encode({'query':'old imported record'})))
    async def complete(*args,**kwargs):
        # In-flight requests are not part of the review list.
        assert [row['session_id'] for row in history(client)] == ['legacy']
        return answer()
    monkeypatch.setattr('serein.api.chat.complete',complete)
    query='时光代理人看到哪了？'
    response=client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':query}]})
    assert response.status_code==200
    reopened=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    rows=history(reopened)
    assert len(rows)==2 and rows[1]['payload']['query']=='old imported record'
    row=rows[0];p=row['payload']
    assert str(row['id'])==response.headers['x-serein-observation-id']
    assert row['session_id']=='main' and p['query']==query
    assert p['request_status']=='completed' and p['recall_state']=='disabled'
    assert p['injected_bucket_ids']==[]
    assert client.get('/v1/host/deliveries').json()=={'status':'ok','items':[],'has_more':False,'next_before_id':0}
    assert 'synthetic-provider-secret' not in json.dumps(rows)
    assert client.get('/api/gateway-injections',params={'before_id':row['id'],'limit':1}).json()['items'][0]['session_id']=='legacy'


def test_existing_failed_pending_and_tool_rows_are_hidden_from_review(deployment):
    settings,client=deployment
    with Store(settings.database) as store,store.transaction():
        for kind,status in [('user_turn','failed'),('tool_continuation','completed'),
                            ('user_turn','upstream_pending'),('user_turn','completed')]:
            store.conn.execute('INSERT INTO injection_debug(session_id,round_id,created_at,payload_json) VALUES (?,?,?,?)',
                               ('synthetic',0,'2026-01-01',encode({'observation_version':1,
                                 'request_kind':kind,'request_status':status,'query':status})))
    result=client.get('/api/gateway-injections',params={'limit':1}).json()
    assert [item['payload']['query'] for item in result['items']]==['completed']
    assert result['has_more'] is False
    assert client.get('/api/gateway-injections',params={'review_ids':'1,2,3,4'}).json()['reviewed_items']==[]


@pytest.mark.parametrize('mode',['selected','no_match','skip'])
def test_recall_and_delivery_agree_without_recording_full_context(deployment,monkeypatch,mode):
    settings,client=deployment;configure(client)
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda _:True)
    selected=['scene:synthetic'] if mode=='selected' else []
    def recall(*args,**kwargs):
        assert 28 < kwargs['deadline_at'] - time.monotonic() <= 30
        return {'selected_refs':selected,'context':'private-source-body' if selected else '',
                'cards':[{'id':selected[0],'title':'A scene','source_kind':'scene','score':.9,'text':'private-source-body'}] if selected else [],
                'routing':{'route':'reading','action':'skip' if mode=='skip' else 'recall','score':.8}}
    monkeypatch.setattr('serein.application.Services.recall',recall)
    async def complete(*args,**kwargs):return answer()
    monkeypatch.setattr('serein.api.chat.complete',complete)
    query='A current question'
    client.post('/v1/chat/completions',json={'messages':[{'role':'system','content':'private-system-prompt'},{'role':'user','content':query}], 'serein':{'memory':True}}).raise_for_status()
    row=history(client)[0];p=row['payload']
    assert p['recall_state']=={'skip':'skipped'}.get(mode,mode)
    assert p['prepared_ids']==p['injected_bucket_ids']==selected
    assert p['semantic_recall_debug']['route']=='reading'
    receipt=client.get('/v1/host/deliveries').json()['items'][0]
    assert receipt['query']==query and receipt['observation_id']==row['id']
    assert receipt['delivered_ids']==selected
    assert recent_deliveries(settings.database,'main')==selected
    assert not any(secret in json.dumps(row) for secret in ['private-source-body','private-system-prompt','synthetic-provider-secret'])


def test_upstream_failure_does_not_claim_injection_or_advance_cooldown(deployment,monkeypatch):
    settings,client=deployment;configure(client)
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda _:True)
    monkeypatch.setattr('serein.application.Services.recall',lambda *a,**k:{'selected_refs':['scene:s'],'context':'selected context','cards':[]})
    async def complete(*args,**kwargs):raise httpx.ConnectError('secret upstream URL must not be logged')
    monkeypatch.setattr('serein.api.chat.complete',complete)
    r=client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':'A failed question'}],'serein':{'memory':True}})
    assert r.status_code==502
    assert history(client)==[]
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM injection_debug').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==0
    assert recent_deliveries(settings.database,'main')==[]


def test_not_ready_is_not_recorded_without_calling_upstream(deployment):
    _,client=deployment;configure(client)
    r=client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':'Not ready'}],'serein':{'memory':True}})
    assert r.status_code==409
    assert history(client)==[]


@pytest.mark.parametrize('ending,expected',[('', False),('data: [DONE]\n\n',True),('data: {"error":{"message":"private upstream detail"}}\n\n',False)])
def test_stream_completion_is_required_for_observed_delivery(deployment,monkeypatch,ending,expected):
    _,client=deployment;configure(client)
    original=httpx.AsyncClient
    raw='data: {"choices":[{"index":0,"delta":{"content":"hello"}}]}\n\n'+ending
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=raw)),**kw))
    client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':'Streaming question'}],'stream':True})
    rows=history(client)
    assert len(rows)==int(expected)
    if expected:
        assert rows[0]['payload']['request_status']=='completed'
    assert 'private upstream detail' not in json.dumps(rows)


def test_tool_continuation_does_not_create_a_recall_observation(deployment,monkeypatch):
    _,client=deployment;configure(client)
    async def complete(*args,**kwargs):return answer()
    monkeypatch.setattr('serein.api.chat.complete',complete)
    messages=[{'role':'user','content':'The original question'},
              {'role':'assistant','tool_calls':[{'id':'call-a','type':'function','function':{'name':'lookup','arguments':'{}'}}]},
              {'role':'tool','tool_call_id':'call-a','content':'Tool result'}]
    client.post('/v1/chat/completions',json={'messages':messages}).raise_for_status()
    assert history(client)==[]
    assert client.get('/v1/host/deliveries').json()['items']==[]


def test_tool_continuation_reuses_context_without_repeating_recall_or_delivery(deployment,monkeypatch):
    settings,client=deployment;configure(client)
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda _:True)
    calls=[]
    def recall(*args,**kwargs):
        calls.append(args[1])
        return {'selected_refs':['scene:synthetic'],'context':'Synthetic remembered fact',
                'cards':[{'id':'scene:synthetic','title':'A scene','source_kind':'scene'}]}
    monkeypatch.setattr('serein.application.Services.recall',recall)
    responses=[]
    async def complete(*args,**kwargs):
        responses.append(1)
        if len(responses)==1:
            return {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[
                {'id':'call-a','type':'function','function':{'name':'lookup','arguments':'{}'}}]}}]}
        return answer()
    monkeypatch.setattr('serein.api.chat.complete',complete)
    user={'role':'user','content':'The original question'}
    first=client.post('/v1/chat/completions',json={'messages':[user],'serein':{'memory':True}})
    first.raise_for_status()
    messages=[user,first.json()['choices'][0]['message'],{'role':'tool','tool_call_id':'call-a','content':'Tool result'}]
    second=client.post('/v1/chat/completions',json={'messages':messages,'serein':{'memory':True}})
    second.raise_for_status()
    assert calls==['The original question']
    assert second.headers['x-serein-context-replayed']=='true'
    assert second.headers['x-serein-observation-id']==''
    assert len(history(client))==1
    assert len(client.get('/v1/host/deliveries').json()['items'])==1
    assert recent_deliveries(settings.database,'main')==['scene:synthetic']


@pytest.mark.parametrize('diagnostic',[
    {'status':'skipped','reason':'hook_deadline_before_reranker'},
    {'status':'no_match','reranker_error':'http_401'},
])
def test_recall_failure_diagnostics_survive_successful_chat(deployment,monkeypatch,diagnostic):
    _,client=deployment;configure(client)
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda _:True)
    monkeypatch.setattr('serein.application.Services.recall',lambda *a,**k:{**diagnostic,'candidate_retrieval':{'candidate_count':6}})
    async def complete(*args,**kwargs):return answer()
    monkeypatch.setattr('serein.api.chat.complete',complete)
    client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':'A recall question'}],'serein':{'memory':True}}).raise_for_status()
    p=history(client)[0]['payload']
    assert p['recall_diagnostics']==diagnostic and p['candidate_count']==6
    assert p['request_status']=='completed' and p['injected_bucket_ids']==[]
