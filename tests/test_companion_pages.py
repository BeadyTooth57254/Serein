import asyncio
from test_public_settings import deployment
from serein.chat_features import prepare, delivered
from serein.compat.persona_engine import PersonaStateEngine
from serein.deployment import read_settings


def test_disabled_memo_editor_persists_without_enabling_tools_or_chat(deployment):
    settings,client=deployment
    body={'title':'合成书单','content':'下次带两本书','repeat_rule':'once'}
    response=client.post('/v1/companion/memos',json=body)
    assert response.status_code==200,response.text
    key=response.json()['id']
    assert client.get('/v1/companion/memos').json()['enabled'] is False
    assert client.post('/v1/extensions/memo_list',json={}).status_code==404
    assert asyncio.run(prepare(settings.database,'test-window','你好',[]))[0]==''
    assert client.put('/v1/companion/memos/'+key,json={**body,'content':'改为一本书'}).status_code==200
    client.patch('/v1/settings',json={'features':{'memos':True}}).raise_for_status()
    context,receipt=asyncio.run(prepare(settings.database,'test-window','你好',[]))
    assert '改为一本书' in context and key in receipt['memo_ids']
    delivered(settings.database,'test-window',receipt)
    assert client.get('/v1/companion/memos').json()['items'][0]['reminder_count']==1
    changed=client.put('/v1/companion/memos/'+key,json={**body,'content':'改为一本书'}).json()
    assert changed['daily_reminder_count']==1 and changed['reminder_count']==1
    client.patch('/v1/companion/memos/'+key,json={'status':'done'}).raise_for_status()
    assert key not in asyncio.run(prepare(settings.database,'test-window','你好',[]))[1]['memo_ids']
    client.delete('/v1/companion/memos/'+key).raise_for_status()
    assert client.get('/v1/companion/memos').json()['items'][0]['status']=='archived'
    client.patch('/v1/companion/memos/'+key,json={'status':'active'}).raise_for_status()
    assert client.get('/v1/companion/memos').json()['items'][0]['content']=='改为一本书'
    daily=client.post('/v1/companion/memos',json={**body,'repeat_rule':'morning_evening','daily_limit':2}).json()
    assert daily['interval_rounds']==0 and daily['daily_limit']==2
    edited=client.put('/v1/companion/memos/'+daily['id'],json={**body,'repeat_rule':'morning_evening','interval_rounds':0,'content':'修改早晚备忘'}).json()
    assert edited['daily_limit']==2 and edited['content']=='修改早晚备忘'


def test_persona_is_read_only_and_reads_runtime_state(deployment):
    settings,client=deployment
    data=client.get('/v1/companion/persona').json()
    assert data['enabled'] is False and data['sessions']==[] and data['session'] is None
    assert client.put('/v1/companion/persona',json={'relationship':{'trust':.73}}).status_code==405
    engine=PersonaStateEngine({'serein_database':settings.database})
    engine.get_current_state('synthetic-window')
    with engine._connect() as conn:
        conn.execute('UPDATE persona_global_state SET trust=.73')
        conn.execute("UPDATE persona_session_state SET tenderness=.65,inner_thought='合成测试状态'")
    data=client.get('/v1/companion/persona?session_id=synthetic-window').json()
    assert data['relationship']['trust']==.73 and data['session']['tenderness']==.65
    assert data['session']['inner_thought']=='合成测试状态'
    assert not read_settings(settings.database)['features']['persona']


def test_editor_reads_do_not_decay_sessions_and_inputs_are_validated(deployment):
    settings,client=deployment
    engine=PersonaStateEngine({'serein_database':settings.database})
    engine.get_current_state('old-window')
    with engine._connect() as conn:
        conn.execute("UPDATE persona_session_state SET tenderness=.9,updated_at='2020-01-01T00:00:00+00:00'")
    first=client.get('/v1/companion/persona?session_id=old-window').json()
    second=client.get('/v1/companion/persona?session_id=old-window').json()
    assert first==second and first['session']['tenderness']==.9
    assert client.post('/v1/companion/memos',json={'title':' ','content':'x'}).status_code==422
    assert client.get('/v1/companion/persona',headers={'Authorization':''}).status_code==401


def test_memo_schedule_snooze_and_scoped_delivery(deployment):
    from datetime import datetime, timedelta
    from serein.compat.memo_store import ReminderStore, LOCAL_TZ
    settings,client=deployment
    stamp=datetime.now(LOCAL_TZ)
    body={'title':'合成日程','content':'检查书单','repeat_rule':'every_n_rounds','interval_rounds':6,
          'start_at':(stamp-timedelta(days=1)).isoformat(),'end_at':(stamp+timedelta(days=1)).isoformat(),
          'next_due_at':(stamp-timedelta(minutes=1)).isoformat(),'daily_limit':3,'max_injections':5,
          'cooldown_minutes':10,'channel':'gateway','session_id':'schedule-window'}
    response=client.post('/v1/companion/memos',json=body)
    assert response.status_code==200,response.text
    key=response.json()['id']
    store=ReminderStore({'serein_database':settings.database})
    due=lambda at,session='schedule-window':store.due(session_id=session,channel='gateway',round_id=10,now=at)
    assert key in [item['id'] for item in due(stamp)]
    assert not due(stamp,'other-window')
    later=client.patch('/v1/companion/memos/'+key,json={'snooze_minutes':30})
    assert later.status_code==200 and not due(stamp+timedelta(minutes=20))
    assert key in [item['id'] for item in due(stamp+timedelta(minutes=31))]
    saved=client.get('/v1/companion/memos').json()['items'][0]
    assert saved['end_at']==body['end_at'] and saved['max_injections']==5 and saved['cooldown_minutes']==10
    for action in [{'snooze_minutes':0},{'status':'done','snooze_minutes':30},{}]:
        assert client.patch('/v1/companion/memos/'+key,json=action).status_code==422
    assert client.patch('/v1/companion/memos/missing',json={'snooze_minutes':30}).status_code==404
    assert client.post('/v1/companion/memos',json={**body,'start_at':'invalid'}).status_code==422
    client.patch('/v1/companion/memos/'+key,json={'status':'archived'}).raise_for_status()
    assert not due(stamp+timedelta(minutes=31))
