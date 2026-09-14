import asyncio
import json
from types import SimpleNamespace

import pytest

from serein.compat.germany.semantic_router import SemanticRecallRouter,build_route_index
from serein.compat.germany.domain_policy import DomainRecallPolicy
from serein.compat.publication import route_data
from serein.recall.routing import route_query


class Embedding:
    enabled=True
    model='fixture'
    query_instruction=''
    max_chars=6000
    client=SimpleNamespace(profile={'model':'fixture','query_instruction':'','max_chars':6000})

    async def embed_query(self,text):return [1.,0.] if text=='hello' else [0.,1.]


def test_published_routes_become_recall_input_without_restart(tmp_path):
    source=tmp_path/'source.json';index=tmp_path/'index.json'
    source.write_text(json.dumps({'schema_version':1,'dataset_version':1,'routes':[
        {'name':'greeting','action':'skip','enabled':True,'threshold':.7,'utterances':[
            {'text':'hello','role':'typical','origin':'manual','status':'published'}]}]}),'utf-8')
    embedding=Embedding()
    asyncio.run(build_route_index(source_path=source,output_path=index,embedding_engine=embedding))
    router=SemanticRecallRouter({'gateway':{'semantic_recall_router':{'mode':'active',
        'routes_path':str(source),'index_path':str(index),'publish_dir':str(tmp_path/'published')}}},embedding)
    query={'profile':embedding.client.profile,'embedding':[1.,0.]}
    assert route_query(None,query,data=route_data(router))['action']=='skip'
    routes=router.dataset_payload()['routes']
    routes[0]['action']='recall'
    asyncio.run(router.publish_dataset(routes=routes,expected_dataset_version=1,confirmation='PUBLISH_SEMANTIC_ROUTES'))
    assert route_query(None,query,data=route_data(router))['action']=='recall'
    with pytest.raises(ValueError,match='version_conflict'):
        asyncio.run(router.publish_dataset(routes=routes,expected_dataset_version=1,confirmation='PUBLISH_SEMANTIC_ROUTES'))


def test_published_domain_updates_are_current_and_versioned(tmp_path):
    config={'gateway':{'domain_recall_policy':{'publish_dir':str(tmp_path)}}}
    writer=DomainRecallPolicy(config);reader=DomainRecallPolicy(config)
    assert reader.policy_for_domain('tech')=='explicit_only'
    payload=writer.dataset_payload()
    for item in payload['policies']:
        if item['key']=='tech':item['policy']='excluded'
    asyncio.run(writer.publish_dataset(policies=payload['policies'],expected_dataset_version=payload['dataset_version'],confirmation='PUBLISH_DOMAIN_RECALL_POLICIES'))
    assert reader.policy_for_domain('tech')=='excluded'


@pytest.fixture
def upgraded(tmp_path):
    import sqlite3
    from serein.config import Settings
    from serein.bootstrap import initialize
    background=tmp_path/'scene-linker-background.yaml'
    background.write_text('scene_linker: {enabled: false}\n','utf-8')
    route_path=tmp_path/'routes.json'
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True,
        embedding={'endpoint':'https://test.invalid/v1/embeddings','api_key':'synthetic'},
        reranker={'endpoint':'https://test.invalid/v1/rerank','model':'fixture','api_key':'synthetic'},
        background={'germany_config_file':str(background)},recall={'routing_file':str(route_path)})
    initialize(settings)
    profile={'model':'fixture','provider_host':'test.invalid','query_instruction':'','document_instruction':'','max_chars':6000}
    with sqlite3.connect(settings.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
    prepared={'format':'serein-query-routes-v1','profile':profile,'dimension':2,'generation':'fixture',
        'policy':{'min_score':.72,'min_margin':.06,'aggregation_top_k':1,'boundary_veto_enabled':True,
                  'boundary_veto_min_score':.62,'boundary_veto_max_deficit':.03},
        'routes':[{'name':'present_chitchat','action':'skip','threshold':.6,'vectors':[[1,0]]}],
        'boundaries':[]}
    route_path.write_text(json.dumps(prepared),'utf-8')
    return settings,background,route_path


@pytest.mark.parametrize('directories',[False,True])
def test_upgrade_chat_uses_prepared_routes_without_publishing_or_reembedding(upgraded,monkeypatch,directories):
    import yaml
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.adapters.embedding import EmbeddingClient
    from serein.compat.publication import recall_policy_data
    from test_public_settings import configure
    settings,background,route_path=upgraded
    if directories:
        background.write_text(yaml.safe_dump({'gateway':{
            'semantic_recall_router':{'publish_dir':str(settings.database.parent/'routes-published')},
            'domain_recall_policy':{'publish_dir':str(settings.database.parent/'domains-published')}}}),'utf-8')
    before=route_path.read_bytes();calls=[]
    monkeypatch.setattr(EmbeddingClient,'_request',lambda *a,**k:pytest.fail('Readiness must not call embedding'))
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    response=client.get('/v1/settings')
    assert response.status_code==200,response.text
    assert response.json()['memory_status']=={'ready':True,'route_source':'prepared','domain_source':'instance','routes':1,'boundaries':0}
    assert recall_policy_data(settings)==(None,None)
    assert configure(client,memory_enabled=True,writer_enabled=False).is_success
    def query(self,text,**kw):
        calls.append('query');return {'profile':self.profile,'embedding':[1,0]}
    monkeypatch.setattr(EmbeddingClient,'query',query)
    async def complete(model,payload,**kw):
        calls.append('chat')
        return {'choices':[{'message':{'role':'assistant','content':'synthetic answer'},'finish_reason':'stop'}]}
    monkeypatch.setattr('serein.api.chat.complete',complete)
    response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'upgrade-fixture'},
        json={'messages':[{'role':'user','content':'hello'}]})
    assert response.status_code==200,response.text
    assert calls==['query','chat']
    assert route_path.read_bytes()==before
    assert not list(settings.database.parent.rglob('active.json'))


def test_background_only_keeps_public_domain_editor_and_custom_exclusions(upgraded,monkeypatch):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.recall.service import Recall
    from serein.adapters.embedding import EmbeddingClient
    settings,_,_=upgraded
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    custom={'key':'reading','label':'Reading','description':'Books','policy':'excluded'}
    assert client.patch('/v1/settings',json={'tagging':{'domains':[custom]}}).is_success
    assert client.get('/api/semantic-recall/domain-policies').json()['policies']==[custom]
    monkeypatch.setattr(EmbeddingClient,'query',lambda self,text,**kw:{'profile':self.profile,'embedding':[1,0]})
    engine=Recall(settings)
    assert engine.run('hello',method='semantic',min_cosine=.5)['status']=='skipped'
    assert engine.policy.domains['reading']=='excluded'


def test_existing_publication_wins_and_invalid_publication_never_falls_back(upgraded,monkeypatch):
    from serein.compat.publication import published_policy,recall_policy_data
    from serein.configured_models import memory_status
    settings,background,_=upgraded
    source=settings.database.parent/'source.json';index=settings.database.parent/'published-index.json'
    source.write_text(json.dumps({'schema_version':1,'dataset_version':1,'routes':[
        {'name':'memory','action':'recall','enabled':True,'threshold':.7,'utterances':[
            {'text':'hello','role':'typical','origin':'manual','status':'published'}]}]}),'utf-8')
    import yaml
    background.write_text(yaml.safe_dump({'gateway':{'semantic_recall_router':{
        'routes_path':str(source),'index_path':str(index)}}}),'utf-8')
    router,domains=published_policy(settings)
    monkeypatch.setattr(router.embedding_engine,'embed_query',Embedding().embed_query)
    asyncio.run(build_route_index(source_path=source,output_path=index,embedding_engine=router.embedding_engine))
    seed=router.dataset_payload()
    asyncio.run(router.publish_dataset(routes=seed['routes'],expected_dataset_version=1,confirmation='PUBLISH_SEMANTIC_ROUTES'))
    payload=domains.dataset_payload()
    for row in payload['policies']:
        if row['key']=='tech':row['policy']='excluded'
    asyncio.run(domains.publish_dataset(policies=payload['policies'],expected_dataset_version=1,confirmation='PUBLISH_DOMAIN_RECALL_POLICIES'))
    # No explicit paths required after initial publication; keep the active files.
    background.write_text('scene_linker: {enabled: false}\n','utf-8')
    data,policies=recall_policy_data(settings)
    assert route_query(None,{'profile':data['profile'],'embedding':[1,0]},data=data)['action']=='recall'
    assert policies['tech']=='excluded' and memory_status(settings)['ready']
    active_source,active_index,_,_=router._active_paths()
    active_source.write_text(active_source.read_text('utf-8')+'\n','utf-8')
    status=memory_status(settings)
    assert status=={'ready':False,'stage':'live_policy','reason':'route_index_stale'}
    assert active_index.exists()
    with pytest.raises(ValueError,match='route_index_stale'):recall_policy_data(settings)


@pytest.mark.parametrize('damage',['missing','profile','dimension','boundary','json','domain'])
def test_readiness_validates_prepared_and_published_inputs_without_provider_calls(upgraded,monkeypatch,damage):
    from serein.configured_models import memory_status
    from serein.adapters.embedding import EmbeddingClient
    settings,_,path=upgraded
    monkeypatch.setattr(EmbeddingClient,'_request',lambda *a,**k:pytest.fail('Offline check called provider'))
    data=json.loads(path.read_text('utf-8'))
    if damage=='profile':data['profile']['model']='other'
    if damage=='dimension':data['dimension']=3
    if damage=='boundary':data['boundaries']=[{'action':'recall','vector':[1]}]
    path.write_text(json.dumps(data),'utf-8')
    if damage=='json':path.write_text('{','utf-8')
    if damage=='missing':path.unlink()
    if damage=='domain':
        directory=settings.database.parent/'domain_recall_policies';directory.mkdir()
        (directory/'active.json').write_text('{','utf-8')
    assert memory_status(settings)['ready'] is False
