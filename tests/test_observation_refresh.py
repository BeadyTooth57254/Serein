import json
import httpx
import pytest
from serein.chat_observation import ChatObservation
from serein.chat_state import recent_deliveries
from serein.core.store import Store, encode
from test_public_settings import deployment, configure


def test_failed_observation_retains_preparation_without_claiming_delivery(deployment):
    settings,client=deployment
    observation=ChatObservation(settings.database)
    observation.start('synthetic','a synthetic question',True)
    observation.prepared(['scene:s'],{'prepared_items':[{'id':'scene:s','title':'Synthetic memory'}]},
                         recall_state='selected',replayed=False)
    observation.finish('interrupted',reason='stream_not_completed')
    # Repeated cleanup cannot turn interruption into successful delivery.
    observation.finish('completed')
    payload=client.get('/api/gateway-injections').json()['items'][0]['payload']
    assert payload['request_status']=='interrupted'
    assert payload['prepared_ids']==['scene:s'] and payload['injected_bucket_ids']==[]
    assert payload['prepared_items'][0]['title']=='Synthetic memory'
    assert payload['observation_revision']==3 and payload['updated_at']
    assert payload['failure_reason']=='stream_not_completed'
    assert recent_deliveries(settings.database,'synthetic')==[]
    assert client.get('/v1/host/deliveries').json()['items']==[]


def test_delta_cursor_keeps_backlog_and_rechecks_old_pending_records(deployment):
    settings,client=deployment
    observations=[]
    for index in range(46):
        observation=ChatObservation(settings.database)
        observation.start('synthetic',f'query {index}',True)
        observations.append(observation)
    head=client.get('/api/gateway-injections',params={'limit':20}).json()
    assert [row['id'] for row in head['items']]==list(range(46,26,-1))
    assert head['next_before_id']==27 and head['has_more']
    observations[0].finish('failed',reason='request_failed')
    page=client.get('/api/gateway-injections',params={'limit':20,'after_id':5,'review_ids':'1,2'}).json()
    assert [row['id'] for row in page['items']]==list(range(6,26))
    assert page['next_after_id']==25 and page['has_more']
    assert {row['id'] for row in page['reviewed_items']}=={1,2}
    assert next(row for row in page['reviewed_items'] if row['id']==1)['payload']['request_status']=='failed'
    next_page=client.get('/api/gateway-injections',params={'limit':20,'after_id':25}).json()
    assert [row['id'] for row in next_page['items']]==list(range(26,46))
    empty=client.get('/api/gateway-injections',params={'after_id':46}).json()
    assert empty['next_after_id']==46 and not empty['items']
    assert client.get('/api/gateway-injections',params={'after_id':5,'before_id':30}).status_code==400


def test_host_delivery_delta_keeps_acknowledgements_read_only(deployment):
    settings,client=deployment
    for index in range(25):
        client.post('/v1/host/deliveries',json={'receipt_id':f'test:{index}',
            'window_id':'synthetic','delivered_ids':['scene:s']}).raise_for_status()
    result=client.get('/v1/host/deliveries',params={'limit':20,'after_id':0,'review_ids':'25'}).json()
    assert [row['id'] for row in result['items']]==list(range(1,21))
    assert result['next_after_id']==20 and result['has_more']
    assert result['reviewed_items'][0]['id']==25
    assert client.get('/api/gateway-injections').json()['items']==[]
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM host_deliveries').fetchone()[0]==25


@pytest.mark.parametrize('ending,expected',[
    ('data: [DONE]\n\n','completed'),
    ('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n','interrupted'),
    ('data: {"error":{"message":"synthetic upstream detail"}}\n\n','failed'),
])
def test_stream_diagnostics_and_success_receipts_remain_separate(deployment,monkeypatch,ending,expected):
    settings,client=deployment
    configure(client)
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda _:True)
    monkeypatch.setattr('serein.application.Services.recall',lambda *a,**kw:{
        'context':'Synthetic private memory body','selected_refs':['scene:s'],
        'cards':[{'id':'scene:s','title':'Synthetic memory','text':'Synthetic private memory body'}]})
    original=httpx.AsyncClient
    raw='data: {"choices":[{"index":0,"delta":{"content":"Synthetic answer"}}]}\n\n'+ending
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,text=raw)),**kw))
    result=client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':'Question'}],
        'stream':True,'serein':{'memory':True}})
    assert 'Synthetic answer' in result.text
    payload=client.get('/api/gateway-injections').json()['items'][0]['payload']
    assert payload['request_status']==expected and payload['prepared_ids']==['scene:s']
    assert payload['injected_bucket_ids']==(['scene:s'] if expected=='completed' else [])
    assert recent_deliveries(settings.database,'main')==(['scene:s'] if expected=='completed' else [])
    assert len(client.get('/v1/host/deliveries').json()['items'])==int(expected=='completed')
    assert 'Synthetic private memory body' not in json.dumps(payload)
    assert 'synthetic upstream detail' not in json.dumps(payload)
