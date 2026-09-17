from copy import deepcopy

import pytest

from serein.recall.reranker_input import memory_document


def doc(body, kind='scene', title='共同经历'):
    return {'kind': kind, 'title': title, 'body_md': body}


def hit(body, text):
    start = body.index(text)
    return {'start_offset': start, 'end_offset': start + len(text), 'text': text}


@pytest.mark.parametrize('kind', ['scene', 'event'])
def test_short_memory_keeps_opening_even_when_only_later_passages_win(kind):
    body = 'Mira和Orion第一次是在读书会上认识的。\n后来一起散步。\n现在仍然喜欢彼此。'
    document = doc(body, kind)
    passages = [hit(body, '现在仍然喜欢彼此。'), hit(body, '后来一起散步。')]
    before = deepcopy((document, passages))
    assert memory_document(document, passages) == f'title: 共同经历\nbody: {body}'
    assert (document, passages) == before


def test_short_scene_keeps_existing_evidence_projection():
    document = doc('见面经历。\n## 评论\n不要送进重排的评论。\n## 正文\n共同的约定。')
    text = memory_document(document, [])
    assert '见面经历。' in text and '共同的约定。' in text
    assert '评论' not in text


def test_long_hits_keep_neighbors_source_order_and_one_request_budget():
    body = ('开头无关内容。' * 300 + '早处上文。早处命中。早处下文。'
            + '中间无关内容。' * 500 + '晚处上文。晚处命中。晚处下文。' + '结尾无关内容。' * 300)
    text = memory_document(doc(body), [hit(body, '晚处命中。'), hit(body, '早处命中。')])
    assert '早处上文。早处命中。早处下文。' in text
    assert '晚处上文。晚处命中。晚处下文。' in text
    assert text.index('早处命中') < text.index('晚处命中')
    assert '\n[...]\n' in text and len(text) <= 4000
    assert text.count('早处命中') == text.count('晚处命中') == 1


def test_overlapping_context_is_merged_without_repeating_evidence():
    body = '远处背景。' * 500 + '第一次见面。此后一起读书。' + '远处背景。' * 500
    text = memory_document(doc(body), [hit(body, '此后一起读书。'), hit(body, '第一次见面。')])
    assert '第一次见面。此后一起读书。' in text
    assert text.count('第一次见面。') == text.count('此后一起读书。') == 1
    assert '[...]' not in text and len(text) <= 4000


def test_long_scene_context_cannot_cross_excluded_sections_or_trust_bad_offsets():
    body = ('真实经历。' * 700 + '命中之前。\n## 评论\n秘密评论。\n## 正文\n命中之后。' + '真实经历。' * 700)
    invalid = {'start_offset': 0, 'end_offset': 5, 'text': '伪造内容。'}
    crossing = hit(body, '命中之前。\n## 评论\n秘密评论。')
    text = memory_document(doc(body), [invalid, crossing, hit(body, '命中之后。'), hit(body, '命中之前。')])
    assert '秘密' not in text and '评论' not in text and '伪造' not in text
    assert text.index('命中之前。') < text.index('命中之后。')
    assert len(text) <= 4000


def test_oversized_passages_share_budget_so_late_best_hit_survives():
    early, late = '早处关键事实。' + '甲' * 2800, '晚处关键事实。' + '乙' * 2800
    body = early + '中间背景。' * 300 + late
    text = memory_document(doc(body, 'event'), [hit(body, late), hit(body, early)])
    assert '早处关键事实。' in text and '晚处关键事实。' in text
    assert text.index('早处关键事实。') < text.index('晚处关键事实。')
    assert len(text) <= 4000


def test_no_passages_falls_back_to_bounded_projected_body():
    body = '开头。\n## 评论\n秘密评论。\n## 正文\n' + '原始经历。' * 1000
    text = memory_document(doc(body), [])
    assert len(text) == 4000 and '秘密评论' not in text


def test_exact_budget_includes_title_and_formatting():
    prefix = 'title: 共同经历\nbody: '
    body = '甲' * (4000 - len(prefix))
    assert memory_document(doc(body), []) == prefix + body
    assert len(memory_document(doc(body + '乙'), [])) == 4000
