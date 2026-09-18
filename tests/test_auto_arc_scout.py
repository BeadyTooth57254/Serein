from serein.compat.germany.narrative_revision_scout import normalize_new_roll_candidates


def test_auto_arc_scout_routes_diary_to_existing_arc_and_rejects_invented_target():
    corridors = [{'seed': {'source_type':'diary','source_id':'7','title':'雨夜',
                           'bound_narrative_ids':[]}, 'candidates': []}]
    rolls = [{'narrative_id':'narrative_rain','title':'雨声','query_cues':['雨夜']}]
    output = {'candidates':[{'seed_source_type':'diary','seed_source_id':'7',
        'target_narrative_id':'narrative_rain','title':'雨声','reason':'延续雨夜生活线',
        'confidence':'high','materials':[{'source_type':'diary','source_id':'7'}]}]}
    routed = normalize_new_roll_candidates(output, corridors, existing_rolls=rolls)
    assert routed[0]['target_narrative_id'] == 'narrative_rain'
    assert routed[0]['source_diary_ids'] == ['7']
    output['candidates'][0]['target_narrative_id'] = 'invented'
    assert normalize_new_roll_candidates(output, corridors, existing_rolls=rolls) == []


def test_auto_arc_scout_requires_two_materials_for_new_collecting_arc():
    corridors = [{'seed': {'source_type':'diary','source_id':'7','title':'搬家日记',
                           'bound_narrative_ids':[]},
                  'candidates':[{'source_type':'scene','source_id':'scene_room','title':'新房间',
                                 'bound_narrative_ids':[]}]}]
    item = {'seed_source_type':'diary','seed_source_id':'7','target_narrative_id':'',
            'title':'新房间','reason':'两份材料形成新的生活线','confidence':'high',
            'materials':[{'source_type':'diary','source_id':'7'},
                         {'source_type':'scene','source_id':'scene_room'}]}
    routed = normalize_new_roll_candidates({'candidates':[item]}, corridors, existing_rolls=[])
    assert routed[0]['source_diary_ids'] == ['7'] and routed[0]['source_scene_ids'] == ['scene_room']
    item['materials'] = item['materials'][:1]
    assert normalize_new_roll_candidates({'candidates':[item]}, corridors, existing_rolls=[]) == []
