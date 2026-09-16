import json
import math

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


@pytest.fixture
def routed_recall(routes, tmp_path, monkeypatch):
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
    embedded, scored = [], []

    class Embedding:
        def __init__(self,*args,**kwargs): pass
        def query(self,text):
            embedded.append(text)
            return dict(query=text,profile=data['profile'],embedding=[1,0])
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient',Embedding)

    def rank(query, documents):
        scored.append((query, documents))
        return {row['ref']: .9 for row in documents}

    engine=Recall(Settings(database,index,embedding={'endpoint':'unused','api_key_env':'unused'},
                           recall={'routing_file':str(path)}),reranker=rank)
    return engine, path, data, embedded, scored


def test_skip_stops_reranking_but_explicit_reading_is_preserved(routed_recall):
    engine, _, _, _, scored = routed_recall
    assert engine.run('日常问候',method='semantic',min_cosine=.5)['status']=='skipped'
    assert engine.run('日常问候',method='semantic',min_cosine=.5,mode='lookup')['selected_refs']==['scene:scene_a']
    assert engine.run('请读《雨天相逢》',method='semantic',min_cosine=.5,mode='lookup')['selected_refs']==['scene:scene_a']
    assert not scored


@pytest.mark.parametrize('route', ['present_chitchat', 'present_reality', '技术闲聊', 'custom_skip'])
def test_published_skip_stops_before_candidate_retrieval(routed_recall, monkeypatch, route):
    engine, path, data, embedded, scored = routed_recall
    data['routes'][0]['name'] = route
    path.write_text(json.dumps(data), encoding='utf-8')

    def unexpected_candidates(*args, **kwargs):
        pytest.fail('A final skip must stop before opening candidate retrieval')

    monkeypatch.setattr('serein.recall.typed_surface.run', unexpected_candidates)
    query = '还记得雨天相逢吗？'
    result = engine.run(query, method='semantic', min_cosine=.5)
    assert embedded == [query] and not scored
    assert result['routing']['action'] == result['pre_candidate_gate']['route_action'] == 'skip'
    assert result['status'] == 'skipped' and result['reason'] == 'published_skip_route'
    assert 'candidate_retrieval' not in result and result['selected_refs'] == []


@pytest.mark.parametrize('case,reason', [
    ('below_threshold', 'uncertain_route'),
    ('small_margin', 'uncertain_route'),
    ('boundary', 'recall_boundary'),
    ('recall_winner', 'recall_route'),
    ('no_routes', 'no_route'),
])
@pytest.mark.parametrize('score', [.9, .64])
def test_final_recall_reaches_body_scoring_without_a_second_intent_gate(routed_recall, case, reason, score):
    engine, path, data, embedded, scored = routed_recall
    if case == 'below_threshold':
        data['routes'][0]['vectors'] = [[.47, math.sqrt(1 - .47 ** 2)]]
    elif case == 'small_margin':
        data['routes'][0]['vectors'] = [[.8, .6]]
        data['routes'][1]['vectors'] = [[.79, math.sqrt(1 - .79 ** 2)]]
    elif case == 'boundary':
        data['boundaries'] = [dict(action='recall', vector=[1, 0])]
    elif case == 'recall_winner':
        data['routes'][0]['vectors'] = [[0, 1]]
        data['routes'][1]['vectors'] = [[1, 0]]
    elif case == 'no_routes':
        data['routes'] = []
    path.write_text(json.dumps(data), encoding='utf-8')

    def rank(query, documents):
        scored.append((query, documents))
        return {row['ref']: score for row in documents}

    engine.reranker = rank
    query = '那个安排还算数吧'
    result = engine.run(query, method='semantic', min_cosine=.5)
    assert embedded == [query]
    assert result['routing']['action'] == 'recall' and result['routing']['reason'] == reason
    assert not result['pre_candidate_gate']['applied']
    assert not result['surface_reranker_gate']['applied']
    assert result['candidate_retrieval']['candidate_count'] == 1
    assert len(scored) == 1 and scored[0][0] == query
    assert result['selected_refs'] == (['scene:scene_a'] if score >= .65 else [])
