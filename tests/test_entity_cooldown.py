import pytest

from serein.config import Settings
from serein.core import Store
from serein.recall.entities import rebuild_entities, candidates
from serein.recall.index import Search, build_index
from serein.recall.policy import RecallPolicy
from serein.recall.query import Query
from serein.recall.service import Recall


@pytest.fixture
def entity_lane(tmp_path, monkeypatch):
    settings = Settings(tmp_path / 'memory.db', tmp_path / 'index.db', recall={'max_cards': 2})
    with Store(settings.database) as store:
        for key in ('a', 'b', 'c', 'd'):
            store.create(key, 'scene', '共同记忆', '窗边一起听雨。')
            source = store.add_source('raw/' + key, '我们读了《共同记忆》。')
            store.bind(key, source)
    build_index(settings.database, settings.index)
    rebuild_entities(settings)
    original_search = Search.search

    def entity_only(self, *args, **kwargs):
        # Simulate a primary retrieval miss; real bound-source entity lookup
        # and all admission, winner selection and delivery filtering still run.
        return {**original_search(self, *args, **kwargs), 'items': []}

    monkeypatch.setattr(Search, 'search', entity_only)
    return settings


@pytest.mark.parametrize('limit,cooled,expected', [
    (2, [], ['scene:a', 'scene:b']),
    (2, ['scene:a'], ['scene:b']),
    (2, ['scene:a', 'scene:b'], []),
    (1, ['scene:a'], []),
])
def test_entity_winners_in_cooldown_leave_empty_slots(entity_lane, limit, cooled, expected):
    settings = entity_lane
    query = '还记得《共同记忆》吗？'
    with Search(settings.database, settings.index) as search:
        handles = candidates(search, Query(query, delivered_ids=tuple(cooled)), RecallPolicy())
    assert [hit['id'] for hit in handles] == ['a', 'b', 'c', 'd']
    result = Recall(settings).run(query, limit=limit, delivered_ids=cooled)
    assert result['selected_refs'] == expected
    assert [card['id'] for card in result['cards']] == expected
    assert result['suppressed'].get('already_delivered', 0) == len(cooled)
    if not expected:
        assert result['additional_context'] == result['full_additional_context'] == ''


def test_entity_explicit_exclusions_and_lookup_are_independent_of_cooldown(entity_lane):
    query = '还记得《共同记忆》吗？'
    with Search(entity_lane.database, entity_lane.index) as search:
        handles = candidates(search, Query(query, exclude_ids=('scene:a', 'b'),
                                          delivered_ids=('scene:c',)), RecallPolicy())
    assert [hit['id'] for hit in handles] == ['c', 'd']
    result = Recall(entity_lane).run(query, mode='lookup', limit=2, delivered_ids=['scene:a', 'scene:b'])
    assert result['selected_refs'] == ['scene:a', 'scene:b']
