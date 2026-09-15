"""Instance settings, stored with the instance database and never in source exports."""

import json
import math
import os
from copy import deepcopy
from urllib.parse import urlsplit
from uuid import uuid4, uuid5, NAMESPACE_URL
from .core.store import Store, encode, Conflict

DEFAULT_IDENTITY = {'user_name': 'User', 'ai_name': 'AI'}
DEFAULT_UPSTREAM = {'base_url': '', 'model': '', 'writer_model': '', 'api_key': '',
                    'writer_enabled': False, 'memory_enabled': False, 'operit_enabled': True}
DEFAULT_FEATURES = {'memos':False, 'persona':False, 'anti_retreat':False, 'window_shadows':False, 'association':False, 'write_context':False, 'relations_auto_accept':False, 'resume':False, 'originals':False, 'favorites':False, 'narrative_tools':False, 'event_to_scene':False, 'index_sync_tool':False}
DEFAULT_RESUME = {'latest_shadow':True, 'recent_events':True, 'favorite_scenes':True, 'selected_memories':False, 'selected_ids':[], 'pending_originals':True}
DEFAULT_DOMAINS = [
    {'key':'relationship','label':'关系','description':'身份、称呼、承诺、边界与沟通方式','policy':'normal'},
    {'key':'intimacy','label':'亲密','description':'身体、欲望与具身互动','policy':'normal'},
    {'key':'inner','label':'内在','description':'内心、情绪、自省与成长','policy':'normal'},
    {'key':'life','label':'生活','description':'日常、兴趣、工作与生活经历','policy':'normal'},
    {'key':'tech','label':'技术','description':'技术、代码、系统与工程','policy':'normal'},
    {'key':'project','label':'项目','description':'持续推进的项目、计划与协作','policy':'normal'},
    {'key':'general','label':'通用','description':'其他无法归入上述主域的经历','policy':'normal'},
]
TASKS = ('chat', 'writer', 'embedding', 'reranker', 'relations', 'dreams', 'narrative_scout', 'event_pipeline',
         'persona', 'anti_retreat', 'track_router', 'event_curator', 'event_writer', 'operit_tagging')


def read_from_store(store):
    row = store.conn.execute("SELECT value_json FROM background_state WHERE name='deployment_settings'").fetchone()
    saved = json.loads(row[0]) if row else {}
    legacy_mode = 'legacy' if any(saved.get('assignments', {}).get(role) for role in ('track_router','event_curator','event_writer')) else 'agent'
    return {'settings_version':saved.get('settings_version',0), 'identity': {**DEFAULT_IDENTITY, **saved.get('identity', {})},
            'upstream': {**DEFAULT_UPSTREAM, **saved.get('upstream', {})},
            'features': {**DEFAULT_FEATURES, **saved.get('features', {})},
            'recall': saved.get('recall', {}),
            'resume': {key:saved.get('resume', {}).get(key, value) for key,value in DEFAULT_RESUME.items()},
            'models': saved.get('models', []), 'upstreams': saved.get('upstreams', []),
            'tagging': saved.get('tagging', {'domains': DEFAULT_DOMAINS}),
            'tagging_version': saved.get('tagging_version', 1),
            'dream': {'main_prompt':'', 'daily_probability':0.4, **saved.get('dream', {})},
            'pipeline': {'auto_enabled':True,'execution_mode':legacy_mode,'max_input_chars':12000,'max_prompt_chars':40000,'timeout_seconds':600, **saved.get('pipeline',{})},
            'assignments': {key:value for key,value in saved.get('assignments', {}).items() if key!='event_evidence'}}


def feature_enabled(database, name):
    return bool(read_settings(database)['features'].get(name,False))


def grouped_upstreams(state):
    """Present legacy connections in the same editor without losing credentials."""
    groups = deepcopy(state['upstreams'])
    route_keys = ('id','label','dimension','query_instruction','document_instruction')
    for model in state['models']:
        connection = {key:value for key,value in model.items() if key not in (*route_keys,'model')}
        group = next((item for item in groups if all(item.get(key, '') == connection.get(key, '')
                     for key in ('base_url','api_key','protocol','prompt_cache','prompt_cache_retention'))
                     and not item.get('api_key_env')), None)
        if group is None:
            host = urlsplit(model['base_url']).hostname or '上游'
            name = '硅基流动' if host in ('api.siliconflow.cn','api.siliconflow.com') else host
            existing = {item['name'] for item in groups}
            suffix = 2
            original = name
            while name in existing:
                name = f'{original} ({suffix})'; suffix += 1
            group = {**connection,'id':'legacy-upstream-'+uuid5(NAMESPACE_URL,model['id']).hex,'name':name,'models':[]}
            groups.append(group)
        group['models'].append({**{key:model[key] for key in route_keys if key in model},'upstream_model':model['model']})
    return groups


def client_model_id(model):
    return model['upstream_name'] + '/' + (str(model.get('label') or '').strip() or model['model'])


def chat_models(state):
    excluded = {state['assignments'].get(task) for task in ('embedding','reranker')}
    return [model for model in configured_models(state) if model['id'] not in excluded]


def configured_models(state):
    """Expand shared upstream credentials into routes, without changing aliases."""
    result = []
    for upstream in grouped_upstreams(state):
        connection = {key:value for key,value in upstream.items() if key not in ('models','id','name','default_model')}
        connection['api_key'] = upstream.get('api_key') or os.environ.get(upstream.get('api_key_env',''), '')
        entries = [({'id':item,'upstream_model':item} if isinstance(item,str) else item) for item in upstream['models']]
        default = upstream.get('default_model')
        if default and not any(item['id']==default for item in entries):
            entries.append({'id':default,'upstream_model':default})
        for entry in entries:
            result.append({**connection, **entry, 'model':entry['upstream_model'],
                           'label':entry.get('label') or entry['upstream_model'], 'upstream_name':upstream['name']})
    return result


def read_settings(database, *, public=False):
    with Store(database, read_only=True) as store:
        result = read_from_store(store)
    if public:
        result['upstreams'] = grouped_upstreams(result)
        result['models'] = []
        result['available_models'] = [{key:item.get(key) for key in ('id','label','model','protocol','upstream_name')}
                                      for item in configured_models(result)]
        upstream = result['upstream']
        upstream['api_key_configured'] = bool(upstream.pop('api_key'))
        for model in result['models']:
            model['api_key_configured'] = bool(model.pop('api_key', ''))
        for upstream in result['upstreams']:
            upstream['api_key_configured'] = bool(upstream.pop('api_key', '') or os.environ.get(upstream.get('api_key_env',''), ''))
    return result


def save_settings(database, changes):
    with Store(database) as store, store.transaction(immediate=True):
        current = read_from_store(store)
        stored=store.conn.execute("SELECT value_json FROM background_state WHERE name='deployment_settings'").fetchone()
        explicit_mode='execution_mode' in (json.loads(stored[0]).get('pipeline',{}) if stored else {}) or 'execution_mode' in changes.get('pipeline',{})
        if changes.get('expected_version') is not None and changes['expected_version'] != current['settings_version']:
            raise Conflict('设置已在其他页面更新，请刷新后重试')
        for section, values in changes.items():
            if section == 'expected_version':continue
            if section == 'tagging':
                if current['tagging'] != values:
                    current['tagging_version'] += 1
                current['tagging'] = values
            elif section == 'models':
                previous = {item['id']: item for item in current['models']}
                current['models'] = [{**previous.get(item['id'], {}), **item} for item in values]
            elif section == 'upstreams':
                previous = {key: item for item in grouped_upstreams(read_from_store(store)) for key in (item.get('id'), item['name']) if key}
                current['upstreams'] = [{**previous.get(item.get('id',item['name']), {}), **item} for item in values]
                for item in current['upstreams']:
                    item.setdefault('id', uuid4().hex)
            else:
                current[section].update(values)
        if current['features']['narrative_tools']:
            current['upstream']['writer_enabled'] = False
        for key in ('direct_threshold','body_candidate_threshold','cue_candidate_threshold'):
            threshold = current['recall'].get(key)
            if key in current['recall'] and (type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1):
                raise ValueError('Recall threshold must be a finite number between 0 and 1')
        from .recall.policy import RecallPolicy
        RecallPolicy.from_config(current['recall'])
        current['assignments'].pop('anti_retreat',None)
        current['assignments'].pop('event_evidence',None)
        if changes.get('features',{}).get('anti_retreat') is False:
            store.conn.execute("UPDATE background_state SET value_json=json_set(value_json,'$.pending',json('{}'),'$.token','','$.running_until',0) WHERE name LIKE 'anti_retreat:%'")
        catalog = configured_models(current)
        known = {item['id'] for item in catalog}
        if len(known) != len(catalog):
            raise ValueError('Client model IDs must be unique; use aliases for models offered by multiple upstreams')
        public_names = [client_model_id(model) for model in chat_models(current)]
        if len(set(public_names)) != len(public_names):
            raise ValueError('Models in an upstream need distinct aliases; upstream names must distinguish their models')
        if any(value and value not in known for value in current['assignments'].values()):
            raise ValueError('A selected model is missing; clear its task assignment before removing it')
        mode=current['pipeline']['execution_mode']
        if mode not in ('legacy','api','agent'):raise ValueError('Unknown Event execution mode')
        if mode=='api':
            for role in ('track_router','event_curator','event_writer'):
                model=next((item for item in catalog if item['id']==current['assignments'].get(role)),None)
                if not model:raise ValueError('API 自动摘要需要为三个阶段选择模型')
                if not model.get('api_key') and urlsplit(model['base_url']).hostname not in ('localhost','127.0.0.1','::1'):
                    raise ValueError('API 自动摘要所选上游缺少访问密钥')
        current['settings_version'] += 1
        if not explicit_mode:current['pipeline'].pop('execution_mode',None)
        store.conn.execute("INSERT INTO background_state(name,value_json) VALUES ('deployment_settings',?) "
                           "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json", (encode(current),))
    return read_settings(database, public=True)


def identity(database):
    return expanded_identity(read_settings(database)['identity'])


def expanded_identity(names):
    return {**names, 'user_display_name': names['user_name'], 'user_aliases': []}


def task_model(database, task, *, requested=''):
    if task == 'anti_retreat':task = 'persona'
    state = read_settings(database)
    catalog = chat_models(state) if task == 'chat' else configured_models(state)
    if task == 'chat' and state['upstreams'] and not requested and not state['assignments'].get('chat'):
        if len(state['upstreams']) != 1 or state['models']:
            raise ValueError('Choose a model from /v1/models when multiple upstreams are configured')
        default = state['upstreams'][0].get('default_model')
        requested = default if any(item['id']==default for item in catalog) else (catalog[0]['id'] if catalog else '')
    key = requested or state['assignments'].get(task)
    if key:
        model = next((item for item in catalog if client_model_id(item) == key), None) if task == 'chat' else None
        model = model or next((item for item in catalog if item['id'] == key), None)
        if model is None:
            matches = [item for item in catalog if item['model'] == key]
            model = matches[0] if len(matches) == 1 else None
        if model is None:
            raise ValueError('The selected model is not configured')
        return model
    if task in ('chat', 'writer') and state['upstream']['base_url']:
        cfg = state['upstream']
        return {**cfg, 'model': (cfg['writer_model'] if task == 'writer' else '') or cfg['model'],
                'protocol': 'openai', 'prompt_cache': '', 'prompt_cache_retention': ''}
    return None
