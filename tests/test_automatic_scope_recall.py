import sqlite3

import pytest

from serein.core import Store
from serein.recall.germany.memory_recall.observed_entities import _query_intent
from test_personal_recall import birthday_recall


@pytest.fixture
def scoped_recall(birthday_recall):
    engine, embedded, ranked = birthday_recall
    root = engine.settings.database.parent
    with sqlite3.connect(root / 'entities.db') as conn:
        conn.execute("ALTER TABLE scope_anchors ADD COLUMN source_kind TEXT DEFAULT 'fixture'")
        conn.execute('ALTER TABLE scope_anchors ADD COLUMN trusted INTEGER DEFAULT 1')
        conn.execute('ALTER TABLE scope_anchors ADD COLUMN arc_count INTEGER DEFAULT 1')
        conn.executemany('INSERT INTO scope_anchors(entity_text,entity_key,arc_key) VALUES (?,?,?)', [
            ('望春湖', '望春湖', 'place:lake'), ('北岸公园', '北岸公园', 'place:park'),
        ])
        conn.executemany('INSERT INTO scope_anchors VALUES (?,?,?,\'fixture\',0,2)', [
            ('野餐地', '野餐地', 'place:lake'), ('野餐地', '野餐地', 'place:park'),
        ])
    with Store(engine.settings.database) as store:
        for name, member in [('lake', 'assistant_birthday'), ('park', 'user_birthday')]:
            store.create(name, 'narrative', name, 'Only an explicit reader may return this narrative body.',
                         metadata={'arc_key': 'place:' + name})
            store.conn.execute("INSERT INTO narrative_materials VALUES (?,1,'fixture','scene',?,'linked','{}')", (name, member))

    # These tests check who gets a scoring opportunity, not model accuracy.
    def rank(query, documents):
        ranked.append((query, documents))
        return {row['ref']: .9 for row in documents}

    engine.reranker = rank
    return engine, embedded, ranked


@pytest.mark.parametrize('marker', ['整体', '完整剧情', '完整故事', '完整经过', '从头', '整条线', '讲讲剧情', '讲讲故事'])
def test_narrative_words_do_not_select_a_reader(marker):
    assert _query_intent(f'{marker}，你生日哪天？')[1] != 'narrative_read'


@pytest.mark.parametrize('query', [
    '不用从头讲，只说你的生日哪天？',
    '我整体记不清了，你的生日是什么时候？',
])
def test_narrative_word_without_arc_still_reaches_relevance_scoring(birthday_recall, query):
    engine, embedded, ranked = birthday_recall
    result = engine.run(query, method='semantic', min_cosine=.5, user_utterance=True)
    assert ranked and embedded == [query]
    assert result['candidate_retrieval']['status'] == 'ok'
    assert result['admission']['mode'] == 'direct_evidence_rerank'
    assert result['selected_refs'] == ['scene:assistant_birthday']


@pytest.mark.parametrize('query', ['望春湖的地址在哪？', '从头讲讲望春湖的故事'])
def test_named_scope_needs_no_magic_recall_phrase(scoped_recall, query):
    engine, _, ranked = scoped_recall
    result = engine.run(query, method='semantic', min_cosine=.5, user_utterance=True)
    assert not result['pre_candidate_gate']['applied']
    assert not result['surface_reranker_gate']['applied']
    assert result['candidate_retrieval']['candidate_count'] == 1
    assert result['candidate_retrieval']['entity_scope']['retrieval_allowed']
    assert result['admission']['mode'] == 'direct_evidence_rerank'
    assert {row['ref'] for row in ranked[0][1]} == {'scene:assistant_birthday'}
    assert all(card['source_kind'] == 'scene' for card in result['cards'])
    assert 'Only an explicit reader' not in result['context']


@pytest.mark.parametrize('query', ['还记得望春湖和北岸公园吗？', '望春湖和北岸公园的地址在哪？', '野餐地的地址在哪？'])
def test_multiple_or_ambiguous_scopes_fall_back_to_global_candidates(scoped_recall, query):
    engine, _, ranked = scoped_recall
    result = engine.run(query, method='semantic', min_cosine=.5, user_utterance=True)
    scope = result['candidate_retrieval']['entity_scope']
    assert scope['status'] == 'global_recall'
    assert scope['scope_anchor'] is None
    assert scope['scope_fallback'] == 'ambiguous_scope_global_event_scene'
    assert scope['candidate_arc_keys'] == ['place:lake', 'place:park']
    assert not result['pre_candidate_gate']['applied']
    assert not result['surface_reranker_gate']['applied']
    assert {row['ref'] for row in ranked[0][1]} == {'scene:assistant_birthday', 'scene:user_birthday'}


def test_scope_hint_never_waives_reranker_threshold(scoped_recall):
    engine, _, _ = scoped_recall
    engine.reranker = lambda query, documents: {row['ref']: .64 for row in documents}
    result = engine.run('从头讲讲望春湖的故事', method='semantic', min_cosine=.5, user_utterance=True)
    assert result['candidate_retrieval']['candidate_count'] == 1
    assert result['selected_refs'] == []
    assert result['admission']['candidates'][0]['reason'] == 'reranker_below_direct_threshold'


def test_generic_reference_without_a_topic_still_needs_context(scoped_recall):
    engine, _, ranked = scoped_recall
    result = engine.run('还记得吗？', method='semantic', min_cosine=.5, user_utterance=True)
    assert result['reason'] == 'global_query_lacks_specific_terms'
    assert not ranked
