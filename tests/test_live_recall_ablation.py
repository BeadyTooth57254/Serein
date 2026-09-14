import json
import sqlite3

from serein.config import Settings
from serein.core.store import Store
from serein.recall.index import build_index
from serein.recall.service import Recall


def test_cue_ablation_removes_only_the_cue_entrance(tmp_path,monkeypatch):
    database,index=tmp_path/'data.db',tmp_path/'index.db'
    with Store(database) as store:
        store.create('scene_test','scene','那个晚上','窗边传来了雨声。',metadata={'scene_cues':['灯光']})
    build_index(database,index)
    profile=dict(model='fixture',provider_host='fixture.invalid',query_instruction='',document_instruction='',max_chars=6000)
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        conn.execute("INSERT INTO vectors VALUES ('scene_test','[1,0]',2)")
    class Embedding:
        def __init__(self,*args,**kwargs):pass
        def query(self,text):return {'query':text,'profile':profile,'embedding':[0,1]}
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient',Embedding)
    seen=[]
    def rank(query,items):
        seen.extend(items)
        return {item['ref']:.9 for item in items}
    recall=Recall(Settings(database,index,embedding={'endpoint':'unused','api_key_env':'unused'}),reranker=rank)
    assert recall.run('灯光',method='semantic',min_cosine=.5)['selected_refs']==['scene:scene_test']
    assert seen[0]['body']=='窗边传来了雨声。'
    assert recall.run('灯光',method='semantic',min_cosine=.5,recall_ablation='without_cues')['selected_refs']==[]
    assert recall.run('灯光',method='semantic',min_cosine=.5,recall_ablation='without_embedding')['selected_refs']==['scene:scene_test']
