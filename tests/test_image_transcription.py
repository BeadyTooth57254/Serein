import asyncio
import json

from serein.api.chat import prepare_image_transcription
from serein.chat_archive import prepare_turn
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p

from test_event_handoff import PNG
from test_public_features import output_for, settings


def configure(settings, *, feature=False):
    changes = {
        'models': [{'id':'vision','model':'agnes-3.0-flash','base_url':'http://127.0.0.1:9/v1'}],
        'assignments': {'image_transcription':'vision'},
    }
    if feature:
        changes['features'] = {'image_transcription':True}
    save_settings(settings.database, changes)


def test_chat_transcription_is_byte_bound_persisted_and_reused(settings, monkeypatch):
    configure(settings, feature=True)
    calls=[]
    async def complete(model, payload):
        calls.append(payload)
        return {'choices':[{'message':{'content':json.dumps({'image_transcriptions':[
            {'input_image':1,'text':'Visible title','unreadable':False}]})}}]}
    monkeypatch.setattr('serein.image_transcription.complete', complete)
    turn=prepare_turn('window',[{'role':'user','content':[{'type':'text','text':'read this'},
        {'type':'image_url','image_url':{'url':PNG}}]}])
    state={'features':{'image_transcription':True}}
    context,receipt=asyncio.run(prepare_image_transcription(settings,turn,state))
    assert 'Visible title' in context and receipt['status']=='complete' and len(calls)==1
    row=raw_archive(settings).get_event(receipt['message_id'])
    assert row['image_transcription_status']=='complete'
    assert row['image_transcription']['items'][0]['text']=='Visible title'
    context,receipt=asyncio.run(prepare_image_transcription(settings,turn,state))
    assert 'Visible title' in context and receipt['status']=='cached' and len(calls)==1


def test_pipeline_uses_separate_image_model_and_persists_transcription(settings, monkeypatch):
    configure(settings)
    save_settings(settings.database, {
        'assignments': {'track_router':'vision','image_transcription':'vision',
                        'event_curator':'vision','event_writer':'vision'},
        'pipeline': {'execution_mode':'api'},
    })
    raw_archive(settings).ingest([
        {'source_event_id':'image','session_id':'books','role':'user','text':'Read the title',
         'created_at':'2025-01-01T00:00:00Z','metadata':{'attachments':[{'kind':'image','url':PNG}]}},
        {'source_event_id':'reply','session_id':'books','role':'assistant','text':'I will read it',
         'created_at':'2025-01-01T00:01:00Z'}],source='test')
    seen=[]
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        seen.append(request['execution']['task'])
        if request.get('transcription_only'):
            output={'image_transcriptions':[{'input_image':index,'text':'Visible title','unreadable':False}
                                             for index,_ in enumerate(request['images'],1)]}
        else:
            output=output_for(request['role'],request)
        return {'choices':[{'message':{'content':json.dumps(output)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert result['events']==1
    assert seen[:4]==['track_router','image_transcription','event_curator','event_writer']
    with Store(settings.database,read_only=True) as store:
        rows=store.conn.execute("SELECT image_transcription_status,image_transcription_json FROM raw_events "
                                "WHERE image_transcription_status='complete'").fetchall()
    assert rows and 'Visible title' in rows[0]['image_transcription_json']
