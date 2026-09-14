import hashlib
import json
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store, encode
from serein.core.personal import Personal
from serein.legacy_migration.scan import scan
from serein.legacy_migration.workflow import Migration
from serein.legacy_migration.history import run_history_repair
from serein.legacy_migration.comments import import_comments
from serein.api.scenes import routes


@pytest.fixture
def setup(tmp_path):
    root=tmp_path/'old';folder=root/'buckets/dynamic';folder.mkdir(parents=True)
    notes=[{'id':'one','author':'Mira','source':'dashboard','created':'2026-01-02T03:04:00+08:00','content':'  Original user note\n'},
           {'id':'two','author':'Atlas','kind':'feel','source':'hold(feel=True)','created':'2026-03-04','original_feel_created':'2026-02-03','content':'AI year ring','valence':0.8},
           {'id':'three','author':'Old Friend','created':'2026-03-05','content':'Unassigned author'}]
    file=folder/'a.md';file.write_text('---\n'+yaml.safe_dump({'id':'a','name':'Original Scene','created':'2026-01-01','comments':notes},allow_unicode=True)+'---\nUnchanged body','utf-8')
    settings=Settings(tmp_path/'new.db',tmp_path/'index.db',writable=True);initialize(settings)
    return root,settings,notes,file


def migrate(settings,plan):
    m=Migration(settings,plan,{'user_name':'Mira','ai_name':'Atlas','aliases':[]})
    try:m.import_bodies();return m.ids['a']
    finally:m.close()


def test_new_migration_projects_notes_preserves_raw_and_body(setup):
    root,settings,notes,file=setup;original=file.read_bytes();plan=scan(root)
    assert plan['summary']['memory_comments']=={'comments':3,'memories':1}
    # Old fingerprint stays byte-for-byte compatible despite the new capture field.
    old_items=[{k:v for k,v in item.items() if k not in ('date','legacy_created','legacy_comments')} for item in plan['items']]
    expected=hashlib.sha256(json.dumps({'items':old_items,'edges':plan['edges'],'skipped':plan['skipped']},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    assert expected==plan['fingerprint']
    key=migrate(settings,plan)
    app=FastAPI();app.include_router(routes(settings,None,[]))
    view=TestClient(app).post('/api/serein/memory-projection',json={}).json()
    # The actual frontend projection must receive the notes, not only an internal receipt.
    text=json.dumps(view,ensure_ascii=False)
    assert 'Original user note' in text and 'AI year ring' in text and 'Old Friend' in text
    with Store(settings.database) as store:
        doc=store.read(key);assert doc['revision']==1 and doc['body_md']=='Unchanged body'
        rows=store.conn.execute("SELECT * FROM personal_records WHERE scope='annotation' ORDER BY created_at").fetchall()
        values=[json.loads(row['payload_json']) for row in rows]
        assert [v['role'] for v in values]==['user','assistant','unknown']
        assert values[0]['content']==notes[0]['content'] and values[1]['createdAt']=='2026-02-03'
        assert rows[1]['created_at']=='2026-02-03'
        assert store.conn.execute('SELECT count(*) FROM evidence_bindings').fetchone()[0]==0
        raw=store.conn.execute("SELECT content FROM import_records WHERE origin LIKE 'ombre-comments:%'").fetchone()[0]
        assert json.loads(raw)==notes
    assert file.read_bytes()==original


def test_rc45_repair_does_not_duplicate_edit_or_resurrect(setup):
    root,settings,_,_=setup;plan=scan(root)
    old_plan={**plan,'items':[{k:v for k,v in item.items() if k!='legacy_comments'} for item in plan['items']]}
    key=migrate(settings,old_plan)
    personal=Personal(settings.database)
    personal.annotate(key,'A new note','Mira','user',annotation_id='new-note')
    report=run_history_repair(settings,plan);assert report['memory_comments']['inserted']==3
    rows=personal.list('annotation')['items'];old=next(r for r in rows if r['value']['role']=='unknown')
    personal.save('annotation',old['key'],old['value'],document_id=key,expected_revision=1,deleted=True)
    edited=next(r for r in rows if r['value']['role']=='assistant')
    personal.save('annotation',edited['key'],{**edited['value'],'content':'Edited on new site'},document_id=key,expected_revision=1)
    again=run_history_repair(settings,plan)['memory_comments']
    assert again['inserted']==0 and again['existing']==3
    current=personal.list('annotation')['items']
    assert len(current)==3 and any(r['value']['content']=='Edited on new site' for r in current)
    with Store(settings.database) as store:assert store.read(key)['revision']==1


def test_unmatched_and_bad_records_are_reported_not_attached_elsewhere(setup):
    root,settings,_,_=setup;plan=scan(root)
    assert import_comments(settings.database,plan)['held'][0]['count']==3
    with Store(settings.database) as store:assert store.conn.execute('SELECT count(*) FROM personal_records').fetchone()[0]==0
    migrate(settings,plan)
    # A different backup with changed original bytes must not attach to the existing Scene.
    plan['items'][0]['source_hash']='different'
    assert import_comments(settings.database,plan)['held']


def test_duplicate_ids_and_malformed_content_keep_original_snapshot(setup):
    root,settings,notes,file=setup
    meta=yaml.safe_load(file.read_text('utf-8').split('---')[1]);meta['comments']=[notes[0],notes[0],{'id':'bad','content':42}]
    file.write_text('---\n'+yaml.safe_dump(meta,allow_unicode=True)+'---\nUnchanged body','utf-8')
    plan=scan(root);migrate(settings,plan)
    report=import_comments(settings.database,plan)
    assert report['inserted']==0 and len(report['held'])==3
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM personal_records').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM import_records').fetchone()[0]==1
