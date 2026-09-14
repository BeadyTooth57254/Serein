import json
import sqlite3

import pytest

from serein.application import Application
from serein.config import Settings
from serein.core import Store
from serein.deployment import save_settings
from serein.recall.index import build_index
from serein.recall.passage_layouts import layout_path, prepare_layouts
from serein.recall.passages import prepare_passages, fill_passages


def cached(settings, key):
    with sqlite3.connect(layout_path(settings.database)) as db:
        return db.execute('SELECT body_hash,min_chars,spans_json FROM layouts WHERE document_id=?',(key,)).fetchone()


def test_write_prepares_once_without_embedding_and_updates_only_changed_body(tmp_path, monkeypatch):
    settings=Settings(tmp_path/'memory.db',writable=True)
    with Store(settings.database):pass
    save_settings(settings.database,{'recall':{'passages_enabled':True}})
    services=Application(settings).services
    request={'kind':'scene','title':'长经历','body_md':'原文。'*200}
    result=services.write('first','save',request)
    key=result['id']; original=cached(settings,key)
    assert original[1]==500 and len(json.loads(original[2]))>1
    from serein.recall import passages
    split=passages.slices
    monkeypatch.setattr(passages,'slices',lambda *a,**kw:pytest.fail('An unchanged body was split again'))
    assert services.write('first','save',request)['id']==key
    save_settings(settings.database,{'recall':{'passage_min_chars':900}})
    services.write('title','save',{**request,'document_id':key,'expected_revision':1,'title':'只改标题'})
    assert cached(settings,key)==original
    build_index(settings.database,tmp_path/'one.sqlite')
    build_index(settings.database,tmp_path/'two.sqlite')
    for index in ('one.sqlite','two.sqlite'):
        prepared=Settings(settings.database,tmp_path/index)
        assert prepare_passages(prepared)['passages_planned']>1
        assert prepare_passages(prepared)['owners_reused_local']==1
    monkeypatch.setattr(passages,'slices',split)
    services.write('body','save',{**request,'document_id':key,'expected_revision':2,'body_md':'改过。'*201})
    assert cached(settings,key)[1:]==(900,'[]')


def test_disabled_passages_neither_split_nor_call_embedding(tmp_path, monkeypatch):
    settings=Settings(tmp_path/'memory.db',writable=True)
    with Store(settings.database) as store:store.create('s','scene','标题','原文。'*200)
    monkeypatch.setattr('serein.recall.passages.slices',lambda *a,**kw:pytest.fail('Disabled feature split a body'))
    monkeypatch.setattr('serein.recall.passages.EmbeddingClient',lambda *a,**kw:pytest.fail('Disabled feature contacted a provider'))
    assert prepare_layouts(settings,['s'])['status']=='disabled'
    assert prepare_passages(settings)['status']=='disabled'
    assert fill_passages(settings)['status']=='disabled'
    assert not layout_path(settings.database).exists()


def test_write_keeps_passage_vector_work_queued_after_lexical_sync(tmp_path):
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.sqlite',writable=True)
    with Store(settings.database):pass
    build_index(settings.database,settings.index)
    save_settings(settings.database,{'recall':{'passages_enabled':True}})
    services=Application(settings).services
    result=services.write('write','save',{'kind':'scene','title':'长经历','body_md':'原文。'*200})
    assert result['index']['status']=='queued'
    assert len(json.loads(cached(settings,result['id'])[2]))>1
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT document_id FROM index_outbox').fetchone()[0]==result['id']


def test_incremental_preparation_does_not_visit_unrelated_documents(tmp_path, monkeypatch):
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.sqlite',recall={'passages_enabled':True})
    with Store(settings.database) as store:
        for key in ('wanted','unrelated'):store.create(key,'scene','标题','原文。'*200)
    build_index(settings.database,settings.index)
    read=Store.read
    def bounded(self, key, **kwargs):
        assert key!='unrelated', 'Incremental indexing read an unrelated memory'
        return read(self,key,**kwargs)
    monkeypatch.setattr(Store,'read',bounded)
    assert prepare_passages(settings,document_ids=['wanted'])['owners_prepared']==1
