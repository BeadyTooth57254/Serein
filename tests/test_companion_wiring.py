import asyncio
import json
import httpx
import pytest
from test_public_settings import deployment
from serein.application import Application
from serein.api.mcp import create_server
from serein.chat_features import current_round, prepare
from serein.compat.memo_store import ReminderStore
from serein.core.store import Store, encode


def configure(client, memos=True):
    client.patch('/v1/settings',json={
        'models':[{'id':'chat','label':'Chat','model':'chat-model','base_url':'http://127.0.0.1:9/v1'},
                  {'id':'persona','label':'Persona','model':'persona-model','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'chat':'chat','persona':'persona'},
        'features':{'persona':True,'memos':memos}}).raise_for_status()


def evaluation():
    return {'event_type':'affection','inner_thought':'刚才答得太快了。其实还想再听两句，不过也不用急着追问，等这段话慢慢说完吧。','surface_trigger':'合成对话',
            'mood_label':'warm','affect_delta':{'tenderness':.15,'security':.12},
            'relationship_event':True,'relationship_delta':{'trust':.02},'confidence':.9}


@pytest.mark.parametrize('memos',[False,True])
def test_real_proxy_persona_cadence_context_and_tool_continuation(deployment,monkeypatch,memos):
    settings,client=deployment;configure(client,memos)
    calls=[];evaluations=[]
    tool={'role':'assistant','content':None,'tool_calls':[{'id':'t1','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
    def handle(request):
        body=json.loads(request.content)
        if body['model']=='persona-model':
            assert not {'thinking','reasoning','enable_thinking'} & body.keys()
            evaluations.append(json.loads(body['messages'][1]['content']))
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(evaluation())}}]})
        calls.append(body)
        answer=tool if len(calls)==3 else {'role':'assistant','content':'合成答复'+str(len(calls))}
        return httpx.Response(200,json={'choices':[{'message':answer,'finish_reason':'tool_calls' if len(calls)==3 else 'stop'}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    history=[]
    for number in (1,2):
        history.append({'role':'user','content':'合成问题'+str(number)})
        response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':history})
        assert response.status_code==200,response.text
        history.append(response.json()['choices'][0]['message'])
    assert evaluations==[] and current_round(settings.database,'w')==2
    store=ReminderStore({'serein_database':settings.database})
    if memos:store.create(title='临时备忘',content='合成工具轮备忘',repeat_rule='once',reminder_id='m1')
    history.append({'role':'user','content':'合成问题3'})
    response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':history})
    assert response.status_code==200
    assert current_round(settings.database,'w')==2 and evaluations==[]
    if memos:assert store.get('m1')['reminder_count']==0
    history.extend([response.json()['choices'][0]['message'],{'role':'tool','tool_call_id':'t1','content':'合成工具结果'}])
    response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':history})
    assert response.status_code==200 and response.headers['x-serein-context-replayed']=='true'
    assert current_round(settings.database,'w')==3 and len(evaluations)==1
    assert evaluations[0]['latest_user_message']=='合成问题3'
    assert [turn['user_message'] for turn in evaluations[0]['recent_conversation_turns']]==['合成问题1','合成问题2']
    assert evaluations[0]['assistant_response']=='合成答复4'
    if memos:assert store.get('m1')['reminder_count']==1 and store.get('m1')['status']=='archived'
    if memos:assert 'm1' in json.dumps(calls[-1],ensure_ascii=False)
    retry=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':history})
    assert retry.status_code==200 and current_round(settings.database,'w')==3
    if memos:assert store.get('m1')['reminder_count']==1
    state=client.get('/v1/companion/persona?session_id=w').json()
    assert state['session']['inner_thought']=='刚才答得太快了。其实还想再听两句，不过也不用急着追问，等这段话慢慢说完吧。' and state['relationship']['trust']>.51
    assert state['events']
    # Changes appear only on the 15-round reminder cadence, not as a whole state dump.
    with Store(settings.database) as db,db.transaction():
        db.conn.execute('UPDATE background_state SET value_json=? WHERE name=?',(encode(14),'feature_round:w'))
    text,_=asyncio.run(prepare(settings.database,'w','继续',history))
    assert '[你的情绪]' in text and 'Current Persona State' not in text and '当前心绪状态' not in text


@pytest.mark.parametrize('ending',['complete','truncated','tool'])
def test_streaming_memo_consumption_and_persona_only_after_final_reply(deployment,monkeypatch,ending):
    settings,client=deployment;configure(client)
    with Store(settings.database) as db,db.transaction():
        db.conn.execute('INSERT INTO background_state VALUES (?,?)',('feature_round:w',encode(2)))
    store=ReminderStore({'serein_database':settings.database})
    store.create(title='流式备忘',content='合成流式提醒',repeat_rule='once',reminder_id='m1')
    evaluations=[]
    def handle(request):
        body=json.loads(request.content)
        if body['model']=='persona-model':
            assert not {'thinking','reasoning','enable_thinking'} & body.keys()
            evaluations.append(body)
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(evaluation())}}]})
        assert '合成流式提醒' in json.dumps(body,ensure_ascii=False)
        delta={'content':'合成流式答案'} if ending!='tool' else {'tool_calls':[{'index':0,'id':'t1','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
        raw='data: '+json.dumps({'choices':[{'index':0,'delta':delta}]})+'\n\n'
        if ending!='truncated':raw+='data: [DONE]\n\n'
        return httpx.Response(200,text=raw,headers={'content-type':'text/event-stream'})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    result=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':[{'role':'user','content':'合成流式问题'}],'stream':True})
    assert result.status_code==200
    success=ending=='complete'
    assert store.get('m1')['reminder_count']==int(success)
    assert len(evaluations)==int(success) and current_round(settings.database,'w')==2+int(success)


def test_memo_tools_expose_schedule_and_use_same_store(deployment):
    settings,client=deployment;configure(client)
    server=create_server(Application(settings))
    catalog={tool.name:tool for tool in asyncio.run(server.list_tools())}
    assert {'start_at','end_at','daily_limit','max_injections','cooldown_minutes'}<=catalog['memo_create'].inputSchema['properties'].keys()
    app=Application(settings);app.refresh_optional()
    tools=app.contributions.tools
    args=dict(title='早晚备忘',content='合成提醒',memo_id='tool-memo',repeat_rule='morning_evening',start_at='2030-01-01',end_at='2030-01-03',max_injections=4)
    row=tools['memo_create'](**args)
    assert row['daily_limit']==2 and row['interval_rounds']==0 and row['source']=='mcp'
    assert tools['memo_create'](**args)['id']==row['id']
    with pytest.raises(Exception):tools['memo_create'](**{**args,'start_at':'2030-01-02'})
    tools['memo_update'](memo_id=row['id'],start_at='2030-01-02',daily_limit=3,content='修改后的合成提醒')
    saved=client.get('/v1/companion/memos').json()['items'][0]
    assert saved['start_at']=='2030-01-02' and saved['daily_limit']==3 and saved['content']=='修改后的合成提醒'
    tools['memo_list']();assert saved['reminder_count']==0
    client.patch('/v1/settings',json={'features':{'memos':False}}).raise_for_status()
    assert not {'memo_create','memo_list','memo_update'}&{tool.name for tool in asyncio.run(server.list_tools())}
