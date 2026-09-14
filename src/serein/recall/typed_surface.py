"""Mixed Scene/Event recall, with Germany's gates and evidence formatting.

The upstream candidate and admission functions are read-only. This adapter owns
storage translation; model configuration and actual delivery remain with callers.
"""

from collections import Counter
from contextlib import closing
from copy import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

from . import scene
from .germany.candidates import CandidateGateway
from .germany.memory_recall.typed_admission_shadow import evaluate_typed_admission_shadow
from .germany.memory_recall.typed_candidate_shadow import rerank_lane_with_freshness
from .index import Search, content_stamp, unit_vector
from .rendering import render
from .legacy_indexes import lexical_index, cue_index
from .germany.memory_recall.fact_event_lexical_shadow import _source_hash as lexical_hash
from ..deployment import read_from_store


class Snapshot:
    def __init__(self, search, query, policy, embedding, cutoff, use_passages, settings):
        self.search, self.query = search, query
        self.objects, self.catalog, self.whole, self.passages = {}, {}, {}, {}
        stored = search.conn.execute("SELECT value FROM settings WHERE key='embedding_profile'").fetchone()
        if stored is None or json.loads(stored[0]) != embedding['profile'] or embedding['query'] != query.text:
            raise ValueError('Query embedding must match the indexed profile and original query')
        dimension = json.loads(search.conn.execute("SELECT value FROM settings WHERE key='embedding_dimension'").fetchone()[0])
        vector = unit_vector(embedding['embedding'], dimension) if dimension is not None else []
        self.cutoff = cutoff
        self.lexical=lexical_index(settings)
        self.cues=cue_index(settings,profile=embedding['profile'])
        stamps = {}
        for row in search.conn.execute("SELECT * FROM documents WHERE kind IN ('scene','event')"):
            obj = search.reader.read(row['id'], with_evidence=False)
            if not obj['readable'] or obj['document']['lifecycle'] != 'active':
                continue
            doc = obj['document']
            if doc['id'] in query.exclude_ids or doc['kind'] + ':' + doc['id'] in query.exclude_ids:
                continue
            if doc['kind'] == 'scene' and (not obj['surface_state']['can_surface'] or scene.domain_rejection(doc, query, policy)):
                continue
            if content_stamp(doc) != row['stamp']:
                continue
            self.objects[doc['id']] = obj
            stamps[doc['id']] = row['stamp']
            body = scene.evidence_text(doc) if doc['kind'] == 'scene' else doc['body_md']
            meta=doc['metadata']
            day=(meta.get('local_date') or meta.get('source_started_at') or '') if doc['kind']=='event' else (
                meta.get('date') or meta.get('created') or doc['created_at'] or meta.get('updated_at') or '')
            self.catalog[doc['id']] = {'owner_kind': doc['kind'], 'title': doc['title'], 'body': body,
                'memory_date': str(day), 'recallable': obj['surface_state']['can_surface']}
        for row in search.conn.execute('SELECT id,embedding FROM vectors'):
            if row['id'] in self.catalog:
                self.whole[row['id']] = round(sum(a*b for a,b in zip(vector,json.loads(row['embedding']))),4)
        if use_passages and search.has_passages:
            for row in search.conn.execute('SELECT * FROM passages WHERE embedding IS NOT NULL'):
                if row['document_id'] not in stamps or row['stamp'] != stamps[row['document_id']]:
                    continue
                self.passages.setdefault(row['document_id'], []).append({k:row[k] for k in
                    ('ordinal','start_offset','end_offset','text') } | {
                    'score': round(sum(a*b for a,b in zip(vector,json.loads(row['embedding']))),4)})
        for rows in self.passages.values():
            rows.sort(key=lambda row:(-row['score'],row['ordinal']))
        self.cue_allowed=set()
        if Path(self.cues.db_path).is_file():
            with closing(self.cues._connect()) as db:
                states={r['scene_id']:r['source_hash'] for r in db.execute('SELECT scene_id,source_hash FROM memory_cue_passage_scene_state')}
            owners=[{'id':key,'title':obj['document']['title'],'cues':obj['document']['metadata'].get('scene_cues')}
                    for key,obj in self.objects.items() if obj['document']['kind']=='scene']
            parts={('scene',key):sorted(rows,key=lambda r:r['ordinal']) for key,rows in self.passages.items()}
            self.cue_allowed={row['scene_id'] for row in self.cues._normalize_scenes(owners,parts)
                              if states.get(row['scene_id'])==self.cues._source_hash(**row)}
        self.lexical_allowed=set()
        if Path(self.lexical.db_path).is_file():
            with closing(self.lexical._connect()) as db:
                states={r['item_id']:r['source_hash'] for r in db.execute('SELECT item_id,source_hash FROM lexical_documents')}
            for key,obj in self.objects.items():
                doc=obj['document']
                if doc['kind']=='event' and states.get(key)==lexical_hash({**doc['metadata'],'item_id':key,
                        'item_type':'event','title':doc['title'],'body':doc['body_md']}):
                    self.lexical_allowed.add(key)

    def whole_search(self, kind, allowed, limit):
        rows = [{'owner_id':key, 'score':score} for key,score in self.whole.items()
                if self.catalog[key]['owner_kind']==kind and (allowed is None or key in allowed)
                and score >= self.cutoff]
        return sorted(rows,key=lambda row:(-row['score'],row['owner_id']))[:limit]

    def search_scene_whole_by_embedding(self, vector, *, scene_ids, top_k):
        return [{'scene_id':row['owner_id'],'score':row['score']} for row in self.whole_search('scene',scene_ids,top_k)]

    def search_by_embedding(self, vector, *, top_k, owner_kinds=None, passages_per_owner=2,
                            allowed_owner_ids=None, memory_kinds=None, allowed_memory_ids=None,
                            allowed_scene_ids=None):
        if memory_kinds is not None:
            return {'matches':[{'memory_id':row['owner_id'],'memory_kind':'event','score':row['score']}
                               for row in self.whole_search('event',allowed_memory_ids,top_k)]}
        if owner_kinds is None:
            allowed=self.cue_allowed if allowed_scene_ids is None else self.cue_allowed&allowed_scene_ids
            return self.cues.search_by_embedding(vector,top_k=top_k,allowed_scene_ids=allowed)
        rows = [{'owner_kind':self.catalog[key]['owner_kind'],'owner_id':key,'score':parts[0]['score'],
                 'passages':parts[:passages_per_owner]} for key,parts in self.passages.items()
                if self.catalog[key]['owner_kind'] in owner_kinds and parts[0]['score'] >= self.cutoff
                and (allowed_owner_ids is None or (self.catalog[key]['owner_kind'],key) in allowed_owner_ids)]
        return {'matches':sorted(rows,key=lambda row:(-row['score'],row['owner_kind'],row['owner_id']))[:top_k]}

    def search_lexical(self, text, *, top_k, memory_kinds, allowed_memory_ids=None):
        allowed=self.lexical_allowed if allowed_memory_ids is None else self.lexical_allowed&set(allowed_memory_ids)
        return self.lexical.search(text,top_k=top_k,memory_kinds=memory_kinds,allowed_memory_ids=allowed)


def scope_members(reader):
    members = {}
    for row in reader.store.conn.execute("SELECT id FROM documents WHERE kind='narrative' AND lifecycle='active'"):
        doc=reader.store.read(row['id']); meta=doc['metadata'].get('legacy_registry',doc['metadata'])
        if not meta.get('arc_key'): continue
        refs=set(); offset=0
        while True:
            page=reader.materials(row['id'],offset=offset,limit=100)
            refs.update((m['kind'],m['id']) for m in page['items'] if m['selection']=='selected'
                        and m['kind'] in ('event','scene') and m['object']['readable'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        members[meta['arc_key']]=refs
    return members


def mixed_candidates(snapshot, found, members, query):
    """Rank valid vectors with a date prior across types, before pool selection."""
    scope_key=(found.get('entity_scope',{}).get('scope_anchor') or {}).get('arc_key')
    allowed=members.get(scope_key,set()) if scope_key else None
    handles={(row['owner_kind'],row['owner_id']):row for lane in found.get('lanes',{}).values()
             for row in lane.get('matches',[])}
    rows=[]
    for key,doc in snapshot.catalog.items():
        owner=(doc['owner_kind'],key)
        if not doc['recallable'] or (allowed is not None and owner not in allowed):continue
        whole=snapshot.whole.get(key)
        parts=snapshot.passages.get(key,[])
        passage=parts[0]['score'] if parts else None
        scores=[score for score in (whole,passage) if score is not None]
        if not scores or max(scores)<snapshot.cutoff:continue
        score=max(scores)
        evidence=parts[:2] if passage is not None and (whole is None or passage>=whole) else [{
            'ordinal':0,'start_offset':0,'end_offset':len(doc['body']),'text':doc['body'],'score':whole}]
        rows.append({**handles.get(owner,{}),'owner_kind':owner[0],'owner_id':key,'title':doc['title'],
                     'memory_date':doc['memory_date'],'score':score,'passages':evidence,
                     'score_components':{k:v for k,v in (('whole',whole),('passage',passage)) if v is not None}})
    return rerank_lane_with_freshness(rows,query=query.text)


def add_related_candidate(snapshot, ranked, rows, query, policy):
    """Give one reviewed Scene neighbor outside the direct pool a scoring chance."""
    links=scene.related_candidates(snapshot.search.reader,
        [row['owner_id'] for row in rows if row['owner_kind']=='scene'], query, policy,
        limit=None, include_delivered=True)
    by_id={link['id']:link for link in links}
    owners={(row['owner_kind'],row['owner_id']) for row in rows}
    for row in ranked:
        if row['owner_kind']=='scene' and row['owner_id'] in by_id and ('scene',row['owner_id']) not in owners:
            # Keep its own body/vector score. The edge is provenance, not a bonus.
            return [*rows, {**row, 'relation_candidate':by_id[row['owner_id']]}]
    return rows


def run(engine, query, result, gate, decision, embedding, *, cutoff, limit, use_passages,
        with_evidence, body_char_limit, delivered_menu_keys, recall_ablation, deadline_at=None,
        strategy='mixed'):
    with Search(engine.settings.database,engine.settings.index) as search:
        association_enabled=read_from_store(search.reader.store)['features']['association']
        snapshot=Snapshot(search,query,engine.policy,embedding,cutoff,use_passages,engine.settings)
        upstream=CandidateGateway()
        upstream.__dict__.update(copy(gate.engine.__dict__))
        if upstream.observed_entity_shadow_index is None:
            del upstream.observed_entity_shadow_index
        else:
            index=upstream.observed_entity_shadow_index
            upstream.observed_entity_shadow_index=SimpleNamespace(resolve_query=index.resolve_query,
                owner_query_matches=index.owner_query_matches,link_candidates=lambda *args:[])
        upstream.passage_candidate_shadow_enabled=True
        upstream._passage_candidate_shadow_catalog=snapshot.catalog
        upstream._passage_candidate_shadow_arc_members=scope_members(search.reader)
        upstream._passage_candidate_shadow_arc_cards={}
        upstream.embedding_engine=snapshot
        upstream.passage_shadow_index=snapshot
        upstream.fact_event_semantic_index=snapshot
        upstream.cue_passage_shadow_index=snapshot
        upstream.fact_event_lexical_shadow_index=SimpleNamespace(search=snapshot.search_lexical)
        if recall_ablation=='without_cues':
            upstream.cue_passage_shadow_index=SimpleNamespace(search_by_embedding=lambda *a,**kw:{'matches':[]})
        if recall_ablation=='without_embedding':
            snapshot.whole.clear();snapshot.passages.clear()
        found=upstream._passage_candidate_shadow_debug(query.text,embedding['embedding'])
        # The old path is retained only for controlled replay, not exposed as a
        # live API option. Production intentionally uses the mixed policy.
        ranked=mixed_candidates(snapshot,found,upstream._passage_candidate_shadow_arc_members,query) if strategy=='mixed' else found.get('candidates',[])
        rows=ranked[:6] if strategy=='mixed' else ranked
        if found.get('status')=='not_retrieved':rows=[]
        scope=found.get('entity_scope',{})
        result['candidate_policy']={**found.get('policy',{}),'selection_strategy':strategy}
        if strategy=='mixed':
            result['candidate_policy'].update(lane_quotas=None,cross_lane_score_comparison=True,
                freshness_rerank='bounded_across_mixed_pool',pool_limit=7 if association_enabled else 6,
                direct_pool_limit=6,relation_pool_limit=int(association_enabled),association_enabled=association_enabled,final_order='reranker_descending',
                vector_floor=None if cutoff == -1 else cutoff)
        result['candidate_retrieval']={k:found[k] for k in ('status','reason','candidate_count','entity_scope') if k in found}
        result['candidate_retrieval']['candidate_count']=len(rows)
        owner_matches=upstream.observed_entity_shadow_index.owner_query_matches(query.text,
            owner_keys={(r['owner_kind'],r['owner_id']) for r in rows}) if hasattr(upstream,'observed_entity_shadow_index') else []
        surface=upstream._typed_surface_reranker_gate(query.text,scope,gate.debug(decision),
                    candidates=rows,owner_entity_matches=owner_matches)
        result['surface_reranker_gate']={**surface,'entity_scope':scope}
        if surface.get('applied') or found.get('status')=='not_retrieved':
            return {**result,'status':'skipped','reason':found.get('reason') or 'daily_surface_without_memory_intent'}
        if strategy=='mixed' and association_enabled:
            rows=add_related_candidate(snapshot,ranked,rows,query,engine.policy)
            result['candidate_retrieval']['candidate_count']=len(rows)
        rows=[row for row in rows if row['owner_kind']!='event' or snapshot.catalog[row['owner_id']]['recallable']]
        admission_scope=scope
        if strategy=='mixed' and scope.get('operator') not in ('narrative_read','exact_evidence'):
            admission_scope={**scope,'operator':'none'}
        admission=evaluate_typed_admission_shadow(query.text,admission_scope,rows,direct_threshold=engine.policy.direct_threshold)
        scores={}
        if admission['mode']=='direct_evidence_rerank' and rows and engine.reranker:
            remaining=deadline_at-time.monotonic() if deadline_at is not None else None
            if remaining is not None and remaining < 1.8:
                return {**result,'status':'skipped','reason':'hook_deadline_before_reranker','admission':admission}
            documents=[{'ref':upstream._typed_owner_ref(row),'title':'', 'body':'',
                        'rerank_text':upstream._typed_reranker_document(row)} for row in rows]
            from ..adapters.reranker import RerankerClient, RerankerProviderError
            try:
                if remaining is not None and isinstance(engine.reranker,RerankerClient):
                    import httpx
                    with httpx.Client(timeout=max(.05,remaining)) as transport:
                        scores=engine.reranker(query.text,documents,client=transport)
                else:
                    scores=engine.reranker(query.text,documents)
            except RerankerProviderError as exc:
                result['reranker_error']=exc.code
            except ValueError:
                result['reranker_error']='provider_score_unavailable'
            admission=evaluate_typed_admission_shadow(query.text,admission_scope,rows,rerank_scores=scores,
                                                       direct_threshold=engine.policy.direct_threshold)
        result['admission']=admission
        result['candidates']=admission['candidates']
        result['candidate_scores']=[{'ref':upstream._typed_owner_ref(row),'title':row['title'],
            'vector_score':row['score'],'score_channels':row.get('score_components',{}),
            'freshness':row.get('freshness'),'candidate_rank_score':row.get('rerank_score'),
            'time_bonus':round((row.get('rerank_score') or 0)-(row.get('score') or 0),4) if row.get('score') is not None else None,
            'reranker_score':scores.get(upstream._typed_owner_ref(row)),
            'candidate_origin':'relation' if row.get('relation_candidate') else 'direct',
            'relation_candidate':row.get('relation_candidate')} for row in rows]
        admitted=set(admission['selected_refs'])
        maximum=min(limit,engine.policy.max_cards)
        if admission['mode']=='timeline_scope_material':
            admitted=set(admission['material_refs'])
            selected=sorted((r for r in rows if upstream._typed_owner_ref(r) in admitted),
                            key=lambda r:(r['memory_date'],upstream._typed_owner_ref(r)))[-maximum:]
        else:
            eligible=[r for r in rows if upstream._typed_owner_ref(r) in admitted]
            if strategy=='mixed' and admission['mode']=='direct_evidence_rerank':
                eligible.sort(key=lambda r:-scores[upstream._typed_owner_ref(r)])
            selected=eligible[:maximum]
        result['pre_cooldown_selected_refs']=[upstream._typed_owner_ref(row) for row in selected]
        suppressed=Counter();hits=[]
        reasons={row['ref']:row['reason'] for row in admission['candidates']}
        for row in selected:
            ref=upstream._typed_owner_ref(row)
            if ref in query.delivered_ids:
                suppressed['already_delivered']+=1;continue
            obj=search.reader.read(row['owner_id'],with_evidence=with_evidence)
            if not obj['readable'] or not obj['surface_state']['can_surface']:
                suppressed['state_changed_before_read']+=1;continue
            if obj['document']['revision']!=snapshot.objects[row['owner_id']]['document']['revision']:
                suppressed['revision_changed_before_read']+=1;continue
            hits.append({'id':row['owner_id'],'kind':row['owner_kind'],'object':obj,'score':row['score'],
                         'method':'cosine','admission':reasons[ref],'freshness':row.get('freshness'),
                         'rerank_score':scores.get(ref),'score_channels':row.get('score_components',{})})
        result['selected_refs']=[hit['kind']+':'+hit['id'] for hit in hits]
        result['pools']={kind:{'method':'cosine','items':[h for h in hits if h['kind']==kind]} for kind in ('event','scene')}
        result['suppressed']=dict(suppressed)
        result['related_candidates']=scene.related_candidates(search.reader,[h['id'] for h in hits if h['kind']=='scene'],query,engine.policy)
        result.update(render(hits,reader=search.reader,body_char_limit=body_char_limit,
                    scope_arc_key=(scope.get('scope_anchor') or {}).get('arc_key',''),delivered_menu_keys=delivered_menu_keys))
        return {**result,'status':'matched' if hits else 'no_match'}
