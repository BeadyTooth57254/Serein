"""Write-time hints use synthetic memories and never create relationships."""

import asyncio
import json
import sqlite3
from dataclasses import replace

from serein.application import Application
from serein.api.mcp import create_server
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index


def setup(tmp_path, *, old_count=1):
    database, index = tmp_path/'memory.db', tmp_path/'index.db'
    with Store(database) as store:
        for number in range(old_count):
            store.create(f'scene_old_{number}', 'scene', f'灯塔停电夜 {number}',
                         '我们在灯塔停电夜点亮蜡烛，一起等天亮。',
                         metadata={'scene_cues':['灯塔停电夜']})
    build_index(database, index)
    return Settings(database, index, writable=True)


def new_scene():
    return {'kind':'scene', 'title':'灯塔停电夜的后来',
            'body_md':'灯塔停电夜之后，我们又在海边说起那支蜡烛。',
            'metadata':{'scene_cues':['灯塔停电夜']}}


def test_opt_in_hint_is_one_read_only_scene_and_no_relation(tmp_path):
    settings = setup(tmp_path)
    services = Application(settings).services
    off = services.write('off', 'save', {'kind':'scene', 'title':'山地植物',
        'body_md':'我们记录了一株高山植物。', 'metadata':{'scene_cues':['山地植物']}})
    assert 'related_scene_candidate' not in off and 'possible_arc' not in off
    save_settings(settings.database, {'features':{'write_context':True}})
    on = services.write('on', 'save', new_scene())
    assert on['related_scene_candidate'] == {
        'id':'scene_old_0', 'title':'灯塔停电夜 0', 'status':'candidate',
        'hint':'可能相关的旧 Scene：灯塔停电夜 0',
        'read_only':True, 'match_basis':'shared_authored_phrase'}
    assert 'possible_arc' not in on
    assert services.write('on', 'save', new_scene())['id'] == on['id']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM scene_relations').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 3
        assert store.read(on['id'])['body_md'] == new_scene()['body_md']


def test_arc_is_only_a_scope_hint_through_an_existing_linked_scene(tmp_path):
    settings = setup(tmp_path)
    services = Application(settings).services
    arc = services.write('arc', 'save', {'kind':'narrative', 'title':'灯塔故事', 'body_md':'一段整理过的故事。',
        'metadata':{'arc_key':'lighthouse'},
        'materials':[{'kind':'scene','id':'scene_old_0','disposition':'linked'}]})
    save_settings(settings.database, {'features':{'write_context':True}})
    on = services.write('new', 'save', new_scene())
    assert on['possible_arc'] == {'id':arc['id'], 'title':'灯塔故事', 'hint':'可能属于 Arc：灯塔故事',
                                 'arc_key':'lighthouse',
                                 'based_on_scene_id':'scene_old_0', 'status':'possible_membership',
                                 'scope_only':True}
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM narrative_materials WHERE target_id=?',
                                  (on['id'],)).fetchone()[0] == 0


def test_ambiguous_or_unsurfaceable_old_scenes_produce_no_hint(tmp_path):
    settings = setup(tmp_path, old_count=2)
    save_settings(settings.database, {'features':{'write_context':True}})
    services = Application(settings).services
    ambiguous = services.write('ambiguous', 'save', new_scene())
    assert 'related_scene_candidate' not in ambiguous and 'possible_arc' not in ambiguous
    with Store(settings.database) as store:
        store.set_manual_surface('scene_old_1', False)
        store.set_manual_surface(ambiguous['id'], False)
    unambiguous = services.write('eligible', 'save', new_scene())
    assert unambiguous['related_scene_candidate']['id'] == 'scene_old_0'
    unrelated = services.write('unrelated', 'save', {'kind':'scene','title':'海底考古',
        'body_md':'考古队测量了一艘沉船。','metadata':{'scene_cues':['海底考古']}})
    assert 'related_scene_candidate' not in unrelated


def test_hint_failure_cannot_report_a_committed_scene_as_failed(tmp_path, monkeypatch):
    settings = setup(tmp_path)
    save_settings(settings.database, {'features':{'write_context':True}})
    def fail(*_args):
        raise RuntimeError('synthetic lookup failure')
    monkeypatch.setattr('serein.recall.write_context.find_write_context', fail)
    services = Application(settings).services
    result = services.write('save-once', 'save', new_scene())
    assert result['status'] == 'saved' and 'related_scene_candidate' not in result
    assert services.write('save-once', 'save', new_scene())['id'] == result['id']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 2


def test_prepared_vectors_can_find_nonlexical_front_context(tmp_path, monkeypatch):
    settings = setup(tmp_path)
    profile = {'model':'synthetic', 'provider_host':'127.0.0.1',
               'document_instruction':'', 'query_instruction':'', 'max_chars':12000}
    with sqlite3.connect(settings.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        conn.execute("INSERT INTO vectors VALUES ('scene_old_0','[1.0,0.0]',2)")
    prepared = replace(settings, embedding={'endpoint':'http://127.0.0.1:9/embeddings','api_key':'test'},
                       reranker={'endpoint':'http://127.0.0.1:9/rerank','model':'synthetic','api_key':'test'})
    monkeypatch.setattr('serein.configured_models.effective_settings', lambda _settings: prepared)
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient.query',
                        lambda self, text, client=None: {'query':text, 'profile':self.profile, 'embedding':[1.0,0.0]})
    monkeypatch.setattr('serein.adapters.reranker.RerankerClient.__call__',
                        lambda self, text, documents, client=None: {documents[0]['ref']:0.91})
    save_settings(settings.database, {'features':{'write_context':True}})
    result = Application(settings).services.write('semantic', 'save', {'kind':'scene',
        'title':'她生日那晚', 'body_md':'我答应以后把每次发现的风景讲给她听。',
        'metadata':{'scene_cues':['生日风景']}})
    assert result['related_scene_candidate']['id'] == 'scene_old_0'
    assert result['related_scene_candidate']['match_basis'] == 'semantic_body_review'


def test_public_mcp_write_scene_returns_the_optional_hint(tmp_path):
    settings = setup(tmp_path)
    save_settings(settings.database, {'features':{'write_context':True}})
    server = create_server(Application(settings))
    scene = new_scene()
    content = asyncio.run(server.call_tool('write_scene', {
        'operation_id':'mcp-scene', 'title':scene['title'], 'content':scene['body_md'],
        'cues':scene['metadata']['scene_cues'], 'date':'2026-09-14'}))
    result = content[1]
    assert result['status'] == 'saved'
    assert result['related_scene_candidate']['id'] == 'scene_old_0'
