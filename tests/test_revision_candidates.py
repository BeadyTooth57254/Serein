import asyncio
import json
from types import SimpleNamespace
import pytest

from test_public_edition import settings
from serein.compat.narratives import narrative_transaction, RevisionInbox
from serein.core import Store
from serein.compat.germany.narrative_revision_scout import (
    build_new_roll_candidate_prompt, normalize_new_roll_candidates, propose_new_roll_candidates,
)


def seed(settings, count=4):
    with Store(settings.database) as store, store.transaction():
        for i in range(count):
            store.create(f'scene_{i}', 'scene', f'Material {i}', f'Original content {i}')


def test_promoted_live_event_keeps_originals_and_retires_hint_without_losing_draft(settings):
    from serein.application import Application
    from serein.compat.events import Events
    from serein.compat.scout import Scout
    from serein.deployment import save_settings
    from test_live_events import item
    events = Events(settings.database, initialize=True)
    key = events.write_many([item(origin='synthetic:promotion')])['items'][0]['item_id']
    seed(settings, count=1)
    hint = {**candidate(('scene_0',)), 'source_event_ids':[key]}
    proposal = consider(settings, hint)[0]
    with narrative_transaction(settings.database, write=True) as rolls:
        RevisionInbox(rolls.store).review(proposal['proposal_id'], action='save_draft',
                                        draft_delta='Keep this authored draft', note='Keep this note')
    save_settings(settings.database, {'features':{'event_to_scene':True}})
    services = Application(settings).services
    original = services.read(key)
    result = services.write('promotion', 'promote_event', {'event_id':key,
        'expected_revision':original['document']['revision'], 'title':'Rain scene', 'body_md':'I remember the rain.'})
    scene = services.read(result['id'])
    assert {e['source_id'] for e in scene['evidence']} == {e['source_id'] for e in original['evidence']}
    assert services.read(key)['document'] == original['document']
    with narrative_transaction(settings.database) as rolls:
        inbox = RevisionInbox(rolls.store)
        assert inbox.list()['items'] == []
        saved = next(p for p in inbox._load()['items'] if p['proposal_id'] == proposal['proposal_id'])
        assert saved['status'] == 'dismissed' and saved['resolution'] == 'event_promoted_to_scene'
        assert saved['draft_delta'] == 'Keep this authored draft' and saved['review_note'] == 'Keep this note'
    assert consider(settings, hint) == []
    inventory = asyncio.run(Scout(settings)._active_narrative_material_inventory())
    assert not any(p['source_type'] == 'event' and p['source_id'] == key for p in inventory)
    assert any(p['source_type'] == 'scene' and p['source_id'] == result['id'] for p in inventory)


def candidate(ids=('scene_0', 'scene_1'), target=''):
    return {'source_scene_ids':list(ids), 'source_event_ids':[], 'title':'Rain story',
            'reason':'A continuing story', 'existing_proposal_id':target, 'latest_date':'2026-09-11'}


def consider(settings, value):
    with narrative_transaction(settings.database, write=True) as rolls:
        return RevisionInbox(rolls.store).consider_new_roll_candidates([value], model='synthetic-model')


def test_accumulate_one_source_keeps_identity_draft_and_deduplicates(settings):
    seed(settings)
    first = consider(settings, candidate())[0]
    key = first['proposal_id']
    with narrative_transaction(settings.database, write=True) as rolls:
        inbox = RevisionInbox(rolls.store)
        inbox.review(key, action='save_draft', draft_delta='Authored draft', note='Keep my note')
    added = consider(settings, {**candidate(('scene_2',), key), 'title':'Model changed its mind'})[0]
    assert added['change'] == 'accumulated'
    assert added['proposal_id'] == key and added['narrative_id'] == first['narrative_id']
    assert added['narrative_title'] == first['narrative_title'] and added['created_at'] == first['created_at']
    assert added['draft_delta'] == 'Authored draft' and added['review_note'] == 'Keep my note'
    assert added['source_scene_ids'] == ['scene_0','scene_1','scene_2']
    assert added['last_added_material_count'] == 1
    assert consider(settings, candidate(('scene_1','scene_2'), key)) == []
    assert consider(settings, {**candidate(), 'title':'Another title'}) == []
    assert consider(settings, candidate(('scene_2','scene_3'))) == []
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM narrative_proposals').fetchone()[0] == 1
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='narrative'").fetchone()[0] == 0


def test_review_or_retirement_during_scout_does_not_reopen_or_add(settings):
    seed(settings)
    first = consider(settings, candidate())[0]
    with narrative_transaction(settings.database, write=True) as rolls:
        RevisionInbox(rolls.store).review(first['proposal_id'], action='dismiss')
    assert consider(settings, candidate(('scene_2',), first['proposal_id'])) == []
    assert consider(settings, {**candidate(), 'title':'Renamed ignored card'}) == []
    with Store(settings.database) as store:
        store.set_lifecycle('scene_3', 'archived')
    assert consider(settings, candidate(('scene_2','scene_3'))) == []


def test_bound_source_is_rejected_before_persist(settings):
    seed(settings)
    with narrative_transaction(settings.database, write=True) as rolls:
        result = rolls.publish(narrative_id='narrative_bound', document='# Story\n\n## 第一人称叙事\n\nWritten.\n\n## 来源账\n\nscene_2\nscene_3\n',
                      title='Story', arc_key='arc:synthetic', expected_revision=0, source_scene_ids=['scene_2','scene_3'])
        assert result['status'] == 'created', result
    assert consider(settings, candidate(('scene_2','scene_3'))) == []


def test_accumulation_limit_is_explicit_and_reconciliation_checks_late_material(settings):
    seed(settings, 501)
    first = consider(settings, candidate())[0]
    with narrative_transaction(settings.database, write=True) as rolls:
        inbox = RevisionInbox(rolls.store)
        raw = inbox._load()
        raw['items'][0]['source_scene_ids'] = [f'scene_{i}' for i in range(500)]
        inbox._save(raw)
    full = consider(settings, candidate(('scene_500',), first['proposal_id']))[0]
    assert full['change'] == 'capacity_reached' and full['accumulation_warning']
    assert len(full['source_scene_ids']) == 500 and 'scene_500' not in full['source_scene_ids']
    assert consider(settings, candidate(('scene_500',), first['proposal_id'])) == []
    with narrative_transaction(settings.database, write=True) as rolls:
        removed = RevisionInbox(rolls.store).reconcile_bound_new_roll_materials(bound_event_ids=set(), bound_scene_ids={'scene_499'})
        assert removed == [first['proposal_id']]


def test_scout_prompt_and_validation_support_explicit_single_material_continuation(settings):
    seed(settings)
    prior = consider(settings, candidate())[0]
    corridors = [{'seed':{'source_type':'scene','source_id':'scene_2','title':'More rain'}, 'candidates':[]}]
    with narrative_transaction(settings.database) as rolls:
        pending = RevisionInbox(rolls.store).candidate_contexts(corridors)
    output = {'candidates':[{'seed_source_type':'scene','seed_source_id':'scene_2', 'title':'Rain',
                           'reason':'Later development of this same story', 'confidence':'high',
                           'existing_proposal_id':prior['proposal_id'],
                           'materials':[{'source_type':'scene','source_id':'scene_2'}]}]}
    captured = []
    async def complete(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    result = asyncio.run(propose_new_roll_candidates(client=client, model='synthetic', corridors=corridors,
                        role_rules='Only derived hints', existing_candidates=pending))
    assert result[0]['existing_proposal_id'] == prior['proposal_id']
    assert result[0]['source_scene_ids'] == ['scene_2']
    assert prior['proposal_id'] in captured[0]['messages'][1]['content']
    assert consider(settings, result[0])[0]['change'] == 'accumulated'
    output['candidates'][0]['existing_proposal_id'] = 'invented'
    assert normalize_new_roll_candidates(output, corridors, pending) == []
    output['candidates'][0]['existing_proposal_id'] = ''
    assert normalize_new_roll_candidates(output, corridors, pending) == []
    prompt = build_new_roll_candidate_prompt(corridors, role_rules='rules', existing_candidates=[{
        **prior, 'proposal_id':f'proposal_{i}', 'source_scene_ids':['x'*200]*500,
        'source_excerpt':'R'*500} for i in range(24)])[1]['content']
    context = prompt.split('<pending_candidates_json>')[1].split('</pending_candidates_json>')[0]
    assert len(context) <= 16000


def test_candidate_material_titles_pagination_and_current_read_access(settings):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    seed(settings)
    first = consider(settings, candidate())[0]
    path = '/api/narrative-revision-inbox/' + first['proposal_id'] + '/materials'
    with TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'}) as client:
        page = client.get(path, params={'limit':1}).json()
        assert page['total'] == 2 and page['next_offset'] == 1
        assert page['items'][0]['title'] == 'Material 0' and 'body_md' not in page['items'][0]
        assert client.get(path, params={'offset':1}).json()['next_offset'] is None
        content = client.get(path, params={'kind':'scene','identifier':'scene_0'}).json()
        assert content['document']['body_md'] == 'Original content 0'
        assert client.get(path, params={'kind':'scene','identifier':'scene_3'}).json()['status'] == 'not_found'
        with Store(settings.database) as store:
            store.set_lifecycle('scene_0', 'deleted')
        hidden = client.get(path, params={'kind':'scene','identifier':'scene_0'}).json()
        assert not hidden['readable'] and hidden['document'] is None
        assert not client.get(path).json()['items'][0]['title']
        assert client.get(path, params={'offset':-1}).status_code == 422
        assert client.get(path, params={'limit':101}).status_code == 422


@pytest.mark.parametrize('fail_first', [False, True])
def test_scan_model_to_inbox_retry_then_explicit_line_creation(settings, monkeypatch, fail_first):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.compat.events import Events
    from serein.deployment import save_settings
    from test_live_events import item

    original = '我们看到《星河列车》第三集，讨论照片里的铁轨。'
    event = item(original, origin='synthetic:arc-scout-chain')
    event['title'] = '《星河列车》的铁轨'
    event['source_refs'][0]['content'] = original
    event_id = Events(settings.database).write_many([event])['items'][0]['item_id']
    with Store(settings.database) as store:
        store.create('scene_train', 'scene', '《星河列车》的照片', '我记得我们从照片聊到铁轨。',
                     metadata={'object_kind': 'scene'})
    save_settings(settings.database, {'models': [{'id': 'scout', 'model': 'synthetic-scout',
        'base_url': 'http://127.0.0.1:9/v1'}], 'assignments': {'narrative_scout': 'scout'}})
    calls = []

    async def complete(model, payload):
        calls.append(payload)
        assert model['model'] == 'synthetic-scout'
        if fail_first and len(calls) == 1:
            raise RuntimeError('synthetic provider failure')
        prompt = payload['messages'][1]['content']
        corridors = json.loads(prompt.split('<keyword_corridors_json>')[1].split('</keyword_corridors_json>')[0])
        corridor = next(row for row in corridors if row['seed']['source_type'] == 'event')
        assert corridor['seed']['source_id'] == event_id
        assert corridor['seed']['source_excerpt'] == original
        assert 'scene_train' in [row['source_id'] for row in corridor['one_hop_candidates']]
        output = {'candidates': [{'seed_source_type': 'event', 'seed_source_id': event_id,
            'title': '列车与照片', 'reason': '沿着照片追看铁轨线索', 'confidence': 'high',
            'materials': [{'source_type': 'event', 'source_id': event_id},
                          {'source_type': 'scene', 'source_id': 'scene_train'}]}]}
        return {'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)}}]}

    monkeypatch.setattr('serein.model_runtime.complete', complete)
    with TestClient(create_app(settings, token='synthetic', live=True),
                    headers={'Authorization': 'Bearer synthetic'}) as client:
        scan = lambda: client.post('/api/narrative-revision-inbox/scan', json={}).json()
        if fail_first:
            failed = scan()
            assert failed['external_scout_status'] == 'error' and not failed['external_input_sha256']
            assert not client.get('/api/narrative-revision-inbox').json()['items']
        result = scan()
        assert result['external_scout_status'] == 'ok' and result['new_roll_hints_created'] == 1
        assert not result['narrative_writes_performed']
        assert client.get('/api/narrative-rolls').json()['items'] == []
        proposals = client.get('/api/narrative-revision-inbox').json()['items']
        assert len(proposals) == 1 and proposals[0]['source_event_ids'] == [event_id]
        assert proposals[0]['source_scene_ids'] == ['scene_train']
        assert scan()['external_scout_status'] == 'unchanged'
        assert len(calls) == (2 if fail_first else 1)
        saved = client.patch('/api/narrative-revision-inbox/' + proposals[0]['proposal_id'],
                             json={'action': 'save_line'})
        assert saved.status_code == 200, saved.text
        line = client.get('/api/narrative-rolls', params={'narrative_id': saved.json()['narrative_id']}).json()
        assert line['body'] == '' and line['publication_status'] == 'collecting'
        assert line['arc_key'] and line['linked_event_ids'] == [event_id]
        assert line['linked_scene_ids'] == ['scene_train']
