import json
import sqlite3
from contextlib import closing
from dataclasses import replace

from serein.compat.entity_scope import update_scope
from serein.compat.germany.observed_entities import ObservedEntityShadowIndex
from test_live_clients import live
from test_live_events import item


def test_new_bound_originals_update_scope_and_unbinding_removes_owner(live,tmp_path):
    settings,client=live
    config=tmp_path/'germany.yaml';config.write_text('{}','utf-8')
    profile=tmp_path/'policy.json'
    profile.write_text(json.dumps({'entity_database':'scope.sqlite'}),'utf-8')
    settings=replace(settings,background={'germany_config_file':str(config)},recall={'germany_policy_file':str(profile)})
    refs=item()['source_refs'];refs[0]['content']='我们今天一起看了《三体》，讨论了很久。'
    response=client.post('/v1/tools/call',json={'name':'write_scene','arguments':{'title':'一起读书','content':'一起读完第一章。','cues':['一起读书'],'evidence_refs':refs}})
    key=response.json()['result'].split('[scene_id:')[1].split(']')[0]
    rows=update_scope(settings)
    assert any(row['owner_id']==key and row['entity_text']=='三体' for row in rows)
    result=client.post('/v1/tools/call',json={'name':'read_scene_evidence','arguments':{'scene_id':key}}).json()['result']
    client.post('/v1/tools/call',json={'name':'unbind_scene_evidence','arguments':{'scene_id':key,'evidence_ids':[result['evidence_refs'][0]['id']]}})
    assert not any(row['owner_id']==key for row in update_scope(settings))
