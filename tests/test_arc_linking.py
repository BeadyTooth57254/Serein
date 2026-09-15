import asyncio

from serein.arc_linking import candidate_arcs, enqueue, initialize, process
from serein.bootstrap import initialize as bootstrap
from serein.compat.events import Events
from serein.compat.narratives import narrative_transaction
from serein.config import Settings
from serein.core.store import Store


def setup(tmp_path):
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True)
    bootstrap(settings)
    with narrative_transaction(settings.database,write=True) as rolls:
        result=rolls.publish(narrative_id='narrative_garden',expected_revision=0,title='雨夜花园',
            document='# 雨夜花园\n\n## 第一人称叙事\n\n- diary:1\n- diary:2\n',
            source_diary_ids=[1,2],publication_status='collecting',
            arc_key='arc:rain-garden',query_cues=['花园','雨夜'],primary_entities=['Serein'])
        assert result['status']=='created'
    result=Events(settings.database).write_many([{'type':'event','title':'花园流程图完成',
        'body':'我们把雨夜花园的公开流程图画完了。','origin_id':'assistant_bridge:arc-test','recallable':True,
        'source_refs':[{'source_system':'test','session_id':'one','message_id':'1','role':'user',
            'created_at':'2026-09-15T10:00:00+08:00','content':'把花园流程图画完了',
            'binding_method':'test'}]}])
    event_id=result['items'][0]['item_id']
    event=Events(settings.database).read(event_id)
    initialize(settings.database)
    with Store(settings.database) as store:enqueue(store.conn,event_id,event['fingerprint'])
    return settings,event_id


def test_linker_reads_event_and_keyword_arc_cards_without_originals(tmp_path):
    settings,event_id=setup(tmp_path);seen=[]
    async def decide(payload):
        seen.append(payload)
        assert payload['event']['body']=='我们把雨夜花园的公开流程图画完了。'
        assert payload['candidate_arcs'][0]['arc_key']=='arc:rain-garden'
        assert 'source_refs' not in str(payload) and 'tagged_entities' not in str(payload)
        return {'action':'attach','arc_key':'arc:rain-garden','reason':'主要经历直接推进雨夜花园'}
    with narrative_transaction(settings.database) as rolls:before=rolls.read('narrative_garden')
    result=asyncio.run(process(settings,invoke_model=decide))
    assert result=={'status':'linked','event_id':event_id,'arc_key':'arc:rain-garden','processed':1}
    assert len(seen)==1
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT arc_key FROM fact_event_arc_links WHERE event_id=?',(event_id,)).fetchone()[0]=='arc:rain-garden'
        assert store.conn.execute('SELECT arc_key FROM event_arc_links WHERE event_id=?',(event_id,)).fetchone()[0]=='arc:rain-garden'
    with narrative_transaction(settings.database) as rolls:
        after=rolls.read('narrative_garden')
        assert after['full_document']==before['full_document'] and after['revision']==before['revision']


def test_no_keyword_candidate_skips_model_and_leaves_event_unbound(tmp_path):
    settings,event_id=setup(tmp_path)
    with Store(settings.database) as store:
        doc=store.read(event_id)
        store.revise(event_id,expected_revision=doc['revision'],title='早餐',body_md='今天吃了面包。',metadata=doc['metadata'])
        event=Events(settings.database).read(event_id)
        store.conn.execute('UPDATE pipeline_arc_links SET event_fingerprint=? WHERE event_id=?',(event['fingerprint'],event_id))
        assert candidate_arcs(store,store.read(event_id))==[]
    async def fail(_payload):
        raise AssertionError('No candidate must not call a model')
    result=asyncio.run(process(settings,invoke_model=fail))
    assert result['status']=='unassigned'
    with Store(settings.database) as store:
        assert not store.conn.execute('SELECT 1 FROM fact_event_arc_links WHERE event_id=?',(event_id,)).fetchone()
