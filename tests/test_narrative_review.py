from test_live_clients import live
from test_live_events import item
from test_live_narratives import seed
from serein.compat.narratives import narrative_transaction, RevisionInbox
from serein.core.reader import Reader
from serein.compat.germany.narrative_materials import narrative_preview_fingerprint


def test_dismiss_withdraws_only_new_arc_binding_and_preserves_written_sources(live):
    settings, client = live
    original, scene = seed(client, settings)
    raw = item('后来我们把雨夜花园画了出来。', origin='synthetic:garden')
    raw['source_refs'][0]['message_id']='8'
    result = client.post('/api/fact-events/batch', json={'items':[raw]}).json()
    added = result['items'][0]
    assert 'item_id' in added,result
    added=client.post('/api/fact-events/read-many',json={'item_ids':[added['item_id']]}).json()['items'][0]
    before = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    payload = {'arc_key':'arc:rain','expected_revision':before['revision'],
        'expected_document_sha256':before['document_sha256'],
        'events':[{'event_id':added['item_id'],'fingerprint':added['fingerprint']}]}
    assert client.post('/api/narrative-arcs/append-event-materials',json=payload).status_code == 200
    with narrative_transaction(settings.database,write=True) as rolls:
        proposal_id = RevisionInbox(rolls.store).consider_stale_roll(before, latest_material={
            'source_id':added['item_id'],'source_type':'event','updated_at':'2099-01-01',
            'title':'雨夜花园','excerpt':raw['body'],'source_sha256':added['fingerprint']}, material_count=3)[0]['proposal_id']
    response = client.post('/v1/tools/call',json={'name':'review_narrative_revision','arguments':{'proposal_id':proposal_id,'action':'dismiss'}})
    assert response.status_code == 200, response.text
    assert response.json()['result']['removed_material_ids']['event_ids'] == [added['item_id']]
    after = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    assert after['full_document'] == before['full_document'] and after['published_at'] == before['published_at']
    assert after['linked_event_ids'] == [original] and after['linked_scene_ids'] == [scene]
    with Reader(settings.database) as reader:
        assert reader.store.read(added['item_id'])['lifecycle'] == 'active'
        assert not reader.store.conn.execute('SELECT 1 FROM event_arc_links WHERE event_id=?',(added['item_id'],)).fetchone()
    payload['expected_revision'] = after['revision']
    assert client.post('/api/narrative-arcs/append-event-materials',json=payload).status_code == 409
    assert client.patch('/api/narrative-revision-inbox/'+proposal_id,json={'action':'dismiss'}).status_code == 200
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').json()['revision'] == after['revision']


def test_save_line_is_idempotent_then_material_save_and_first_body_publish(live):
    settings,client = live
    event,scene = seed(client,settings)
    with narrative_transaction(settings.database,write=True) as rolls:
        inbox=RevisionInbox(rolls.store)
        inbox._save({'items':[{'proposal_id':'nrev_test','narrative_id':'candidate_test',
            'proposal_kind':'new_roll_candidate','status':'pending','narrative_title':'雨声叙事线',
            'source_event_ids':[event],'source_scene_ids':[scene]}]})
    path='/api/narrative-revision-inbox/nrev_test'
    first=client.post('/v1/tools/call',json={'name':'review_narrative_revision','arguments':{'proposal_id':'nrev_test','action':'save_line'}})
    assert first.status_code==200,first.text
    key=first.json()['result']['narrative_id']
    assert client.patch(path,json={'action':'write'}).json()['narrative_id']==key
    read=lambda:client.get('/api/narrative-rolls?narrative_id='+key).json()
    line=read()
    assert line['body']=='' and line['publication_status']=='collecting' and line['revision']==1
    materials={'event_ids':[event],'scene_ids':[scene],'diary_ids':[],'darkroom_ids':[],'upload_ids':[]}
    saved=client.post('/api/narrative-rolls/save-materials',json={'narrative_id':key,'expected_revision':1,'material_ids':materials})
    assert saved.status_code==200,saved.text
    assert read()['body']=='' and read()['publication_status']=='collecting'
    line=read()
    preview=client.post('/api/narrative-rolls/preview-input',json={'narrative_id':key,'mode':'rewrite',
        'expected_revision':line['revision'],'expected_document_sha256':line['document_sha256']})
    assert preview.status_code==200,preview.text
    seal=preview.json();body='我把那天窗边的雨声写在这里。'
    saved=client.post('/api/narrative-rolls/save-body',json={'narrative_id':key,'body':body,
        'expected_revision':line['revision'],'expected_document_sha256':line['document_sha256'],
        'proposed_material_ids':seal['proposed_material_ids'],'expected_material_snapshot_sha256':seal['material_snapshot_sha256'],
        'preview_fingerprint':narrative_preview_fingerprint(narrative_id=key,revision=line['revision'],
            document_sha256=line['document_sha256'],body=body,material_snapshot_sha256_value=seal['material_snapshot_sha256'])})
    assert saved.status_code==200,saved.text
    assert read()['body']==body and read()['publication_status']=='reviewed'
    inbox=client.get('/api/narrative-revision-inbox?status=all').json()['items']
    assert next(p for p in inbox if p['proposal_id']=='nrev_test')['resolution']=='written'


def test_published_material_is_not_withdrawn_by_a_freshness_hint(live):
    settings,client=live
    event,_=seed(client,settings)
    before=client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    with narrative_transaction(settings.database,write=True) as rolls:
        inbox=RevisionInbox(rolls.store)
        proposal_id=inbox.consider_stale_roll(before,latest_material={'source_type':'event','source_id':event,
            'updated_at':'2099-01-01','source_sha256':'test'},material_count=2)[0]['proposal_id']
    result=client.patch('/api/narrative-revision-inbox/'+proposal_id,json={'action':'dismiss'})
    assert result.status_code==200
    assert result.json()['removed_material_ids']=={'event_ids':[],'scene_ids':[]}
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').json()==before


def test_material_only_save_never_erases_a_collecting_roll_with_body(live):
    settings,client=live
    event,scene=seed(client,settings)
    with narrative_transaction(settings.database,write=True) as rolls:
        before=rolls.read('narrative_test')
        result=rolls.publish(narrative_id='narrative_test',document=before['full_document'],
            expected_revision=before['revision'],title=before['title'],arc_key=before['arc_key'],
            source_event_ids=[event],source_scene_ids=[scene],publication_status='collecting')
        assert result['status']=='updated',result
    before=client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    assert before['publication_status']=='collecting' and before['body'].strip()
    response=client.post('/api/narrative-rolls/save-materials',json={
        'narrative_id':'narrative_test','expected_revision':before['revision'],
        'material_ids':{'event_ids':[event],'scene_ids':[scene]}})
    assert response.status_code==409 and response.json()['reason']=='requires_collecting_line'
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').json()==before
