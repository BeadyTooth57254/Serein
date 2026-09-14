import json

import pytest

from serein.config import Settings
from serein.core import Store
from serein.recall.index import build_index
from serein.recall.routing import route_query
from serein.recall.service import Recall


@pytest.fixture
def routes(tmp_path):
    path = tmp_path / 'routes.json'
    profile = dict(model='test', provider_host='test.invalid', query_instruction='', document_instruction='', max_chars=6000)
    data = {'format':'serein-query-routes-v1', 'profile':profile, 'dimension':2, 'generation':'test-v1',
            'policy':dict(min_score=.72,min_margin=.06,aggregation_top_k=1,boundary_veto_enabled=True,
                          boundary_veto_min_score=.62,boundary_veto_max_deficit=.03),
            'routes':[dict(name='present_chitchat',action='skip',threshold=.6,vectors=[[1,0]]),
                      dict(name='memory',action='recall',threshold=.7,vectors=[[0,1]])], 'boundaries':[]}
    path.write_text(json.dumps(data), encoding='utf-8')
    return path,data


def test_published_skip_margin_and_recall_boundary(routes):
    path,data = routes
    def query(vector):
        return route_query(path, dict(profile=data['profile'],embedding=vector))
    assert query([1,0])['action'] == 'skip'
    assert query([0,1])['action'] == 'recall'
    assert query([1,.99])['reason'] == 'uncertain_route'
    data['boundaries'] = [dict(action='recall', vector=[1,0])]
    path.write_text(json.dumps(data),encoding='utf-8')
    assert query([1,0])['reason'] == 'recall_boundary'
    with pytest.raises(ValueError, match='profile'):
        route_query(path,dict(profile={},embedding=[1,0]))


def test_skip_stops_reranking_but_explicit_reading_is_preserved(routes,tmp_path,monkeypatch):
    import sqlite3
    path,data=routes
    database,index=tmp_path/'data.db',tmp_path/'index.db'
    with Store(database) as store:
        store.create('scene_a','scene','雨天相逢','当年的正文')
    build_index(database,index)
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(data['profile']),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        conn.execute("INSERT INTO vectors VALUES ('scene_a','[1,0]',2)")
    class Embedding:
        def __init__(self,*args,**kwargs): pass
        def query(self,text): return dict(query=text,profile=data['profile'],embedding=[1,0])
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient',Embedding)
    def unavailable(*args): pytest.fail('Reranker should not run for skipped chatter or explicit reads')
    engine=Recall(Settings(database,index,embedding={'endpoint':'unused','api_key_env':'unused'},
                           recall={'routing_file':str(path)}),reranker=unavailable)
    assert engine.run('日常问候',method='semantic',min_cosine=.5)['status']=='skipped'
    assert engine.run('日常问候',method='semantic',min_cosine=.5,mode='lookup')['selected_refs']==['scene:scene_a']
    # Explicit lookup still bypasses automatic admission. Surface recall now
    # uses the old body reranker even when the complete title was mentioned.
    assert engine.run('请读《雨天相逢》',method='semantic',min_cosine=.5,mode='lookup')['selected_refs']==['scene:scene_a']

    # A published technical-chatter skip is not one of Germany's two early
    # surface gates: qualifying body evidence must still reach reranking.
    data['routes'][0]['name'] = '技术闲聊'
    path.write_text(json.dumps(data), encoding='utf-8')
    scored = []
    def high_score(query, documents):
        scored.extend(documents)
        return {row['ref']:.9 for row in documents}
    engine.reranker = high_score
    result = engine.run('日常问候', method='semantic', min_cosine=.5)
    assert result['selected_refs'] == ['scene:scene_a'] and scored
    engine.reranker = lambda query, documents: {row['ref']:.2 for row in documents}
    assert engine.run('日常问候', method='semantic', min_cosine=.5)['status'] == 'no_match'
