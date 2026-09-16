"""Durable API/agent transport for the verified Bridge Event contracts."""
import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from ..core.store import Store, Conflict, encode, digest, now
from ..deployment import identity, task_model, read_settings
from .pipeline_limits import blocks, allowed_ids
from ..compat.events import Events, reference_blockers
from .pipeline_rules import dialogue_units, dialogue_unit_is_complete, normalize_event_track_message_output, flushable_dialogue_units
from . import pipeline_latest as latest
from .pipeline_config import snapshot, execution
from .pipeline_images import freeze_images, verify_images, bind_transcriptions, decision, expire_completed_media
from . import pipeline_tracks as track_state

ROLES=('track_router','event_curator','event_writer')
TZ=timezone(timedelta(hours=8))
CONTRACT='public-event-message-tracks-v4'


def initialize(database):
    with Store(database) as store:
        store.conn.executescript('''
            CREATE TABLE IF NOT EXISTS raw_processing(raw_id INTEGER PRIMARY KEY,operation_id TEXT NOT NULL,outcome TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_batches(id TEXT PRIMARY KEY,scope TEXT NOT NULL,input_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',result_json TEXT);
            CREATE TABLE IF NOT EXISTS pipeline_jobs(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,role TEXT NOT NULL,request_json TEXT NOT NULL,output_json TEXT,UNIQUE(batch_id,role));
            CREATE TABLE IF NOT EXISTS pipeline_tracks(id TEXT PRIMARY KEY,scope TEXT NOT NULL,card_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_track_events(track_id TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(track_id,event_id));
            CREATE TABLE IF NOT EXISTS pipeline_routes(raw_id INTEGER PRIMARY KEY,route_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_event_details(event_id TEXT PRIMARY KEY,details_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_schedule(day TEXT PRIMARY KEY,completed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS pipeline_attempts(id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,attempt INTEGER NOT NULL,
                created_at TEXT NOT NULL,output_text TEXT NOT NULL,error TEXT NOT NULL);
        ''')
        from ..imports import archive_imported_originals
        archive_imported_originals(store.conn)
        # Retire only the frozen plan that mixed in archive-only imports. Other
        # originals remain eligible for a fresh plan; never mark the whole batch.
        store.conn.execute("""UPDATE pipeline_batches SET status='superseded_import_boundary'
            WHERE status IN ('pending','routing_only') AND EXISTS (
                SELECT 1 FROM json_each(input_json,'$.routing_messages') m
                JOIN raw_processing p ON p.raw_id=json_extract(m.value,'$.id')
                WHERE p.outcome='archived_only')""")
        # Earlier preview jobs used a different output protocol. Keep the records,
        # restart only unfinished batches; already settled originals stay settled.
        store.conn.execute("UPDATE pipeline_batches SET status='superseded_protocol' WHERE status='pending' AND json_extract(input_json,'$.contract') IS NULL")
        store.conn.execute("UPDATE pipeline_batches SET status='superseded_protocol' WHERE status='pending' AND json_extract(input_json,'$.contract')<>?",(CONTRACT,))
        expire_completed_media(store)


def message(row):
    row=dict(row);session=str(row.get('session_id') or '')
    row['metadata']=row.get('metadata') or json.loads(row.get('metadata_json') or '{}')
    original=row['metadata'].get('original_message') or {}
    if original.get('attachments') and not row['metadata'].get('attachments'):
        row['metadata']={**row['metadata'],'attachments':original['attachments']}
    row['content']=row.get('text',row.get('content',''))
    row['original_session_id']=row.get('original_session_id',session)
    row['session_id']=int(digest(encode([row.get('source'),session]))[:12],16)
    return row


def rules(role,database):
    with latest.identity_scope(identity(database)):return latest.materialize_agent_rules(role)


def new_batch(database,include_recent,clock=None):
    policy=read_settings(database)['pipeline']
    current=(clock or datetime.now(timezone.utc)).astimezone(TZ)
    watermark=current if include_recent else current.replace(hour=3,minute=0,second=0,microsecond=0)
    if not include_recent and current<watermark:return None
    cutoff=watermark-timedelta(minutes=20)
    with Store(database) as store,store.transaction(immediate=True):
        old=store.conn.execute("SELECT * FROM pipeline_batches WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
        if old:
            old_data=json.loads(old['input_json'])
            if len(blocks(old_data['messages'],policy['max_input_chars']))<=1:
                return dict(old)
            # Keep accepted Router output before retiring an oversized unfinished batch.
            for row in store.conn.execute("SELECT request_json,output_json FROM pipeline_jobs WHERE batch_id=? AND role LIKE 'track_router%' AND output_json IS NOT NULL ORDER BY rowid",(old['id'],)):
                request=json.loads(row['request_json'])
                assignments,cards,_=normalize_event_track_message_output(json.loads(row['output_json']),request['messages'],request['active_tracks'],
                    session_id=old_data['scope'],next_track_ordinal=request.get('next_track_ordinal',track_state.next_ordinal(old_data['scope'],request['active_tracks'])))
                for card in cards:store.conn.execute('INSERT OR IGNORE INTO pipeline_tracks VALUES (?,?,?)',(card['track_id'],old_data['scope'],encode(card)))
                for a in assignments:store.conn.execute('INSERT OR IGNORE INTO pipeline_routes VALUES (?,?)',(a['source_message_id'],encode(a)))
            store.conn.execute("UPDATE pipeline_batches SET status='superseded_input_budget' WHERE id=?",(old['id'],))
        complete_upload=''
        if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='file_imports'").fetchone():
            complete_upload=" AND (json_extract(r.metadata_json,'$.import_upload_id') IS NULL OR json_extract(r.metadata_json,'$.import_upload_id') IN (SELECT id FROM file_imports WHERE cursor=json_array_length(payload_json,'$.entries')))"
        scopes=store.conn.execute('SELECT DISTINCT r.source,r.session_id FROM raw_events r WHERE NOT EXISTS (SELECT 1 FROM raw_processing p WHERE p.raw_id=r.id)'+complete_upload+' ORDER BY r.id').fetchall()
        for source,session in scopes:
            rows=[message(row) for row in store.conn.execute('SELECT r.* FROM raw_events r WHERE source=? AND session_id=? AND NOT EXISTS (SELECT 1 FROM raw_processing p WHERE p.raw_id=r.id)'+complete_upload+' ORDER BY r.id',(source,session))]
            eligible=[r for r in rows if datetime.fromisoformat(r['created_at'].replace('Z','+00:00'))<=watermark]
            chunks=blocks(eligible,policy['max_input_chars'])
            for chunk_index,eligible in enumerate(chunks):
                if chunk_index+1<len(chunks):
                    following=dialogue_units(chunks[chunk_index+1])[0]
                    if not dialogue_unit_is_complete(following):
                        eligible=[*eligible,*following]  # A later correction must remain readable, never owned.
                stable=[];parked=[]
                # Reply envelopes determine completion / parked tails, while the latest
                # Router supplies individual messages as Curator ownership atoms.
                for unit in dialogue_units(eligible):
                    ready=dialogue_unit_is_complete(unit) and (include_recent or datetime.fromisoformat(unit[-1]['created_at'].replace('Z','+00:00'))<=cutoff)
                    (stable if ready else parked).extend(unit)
                if not stable:continue
                scope=digest(encode([source,session]))[:20]
                with latest.identity_scope(identity(database)):
                    tracks,ordinal=track_state.load_tracks(store,source,session,eligible[0]['id'],message)
                recent=[message(row) for row in store.conn.execute('SELECT * FROM raw_events WHERE source=? AND session_id=? AND id<? ORDER BY id DESC LIMIT 6',(source,session,eligible[0]['id']))][::-1]
                data={'contract':CONTRACT,'input_policy':policy,'messages':stable,'parked':parked,'routing_messages':eligible,'tracks':tracks,'next_track_ordinal':ordinal,'scope':scope,'source':source,'recent':recent,'day':watermark.date().isoformat()}
                key='pipeline:'+digest(encode(data))
                existing=store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(key,)).fetchone()
                if existing:continue
                store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json) VALUES (?,?,?)',(key,scope,encode(data)))
                return dict(store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(key,)).fetchone())
    return None


def route_result(data,output):
    if output.get('_public_normalized'):
        return output['assignments'],output['tracks'],output.get('next_track_ordinal',track_state.next_ordinal(data['scope'],output['tracks']))
    return normalize_event_track_message_output(output,data['routing_messages'],data['tracks'],session_id=data['scope'],next_track_ordinal=data.get('next_track_ordinal',track_state.next_ordinal(data['scope'],data['tracks'])))


async def route_batch(database,batch,data,runner):
    chunks=blocks(data['routing_messages'],read_settings(database)['pipeline']['max_input_chars'])
    cards=list(data['tracks']);assignments=[];ordinal=data.get('next_track_ordinal',track_state.next_ordinal(data['scope'],cards));prior=list(data['recent'])
    for index,block in enumerate(chunks):
        bounded={**data,'routing_messages':block,'tracks':track_state.parked(cards),'next_track_ordinal':ordinal,'recent':prior[-6:]}
        prompt_batch={**batch,'input_json':encode(bounded)}
        output=await job(database,batch,request_for(database,prompt_batch,'track_router'),f'track_router:{index}',runner)
        routed,updates,ordinal=normalize_event_track_message_output(output,block,cards,session_id=data['scope'],next_track_ordinal=ordinal)
        with latest.identity_scope(identity(database)):
            cards=track_state.update_cards(cards,routed,updates,block,data['scope'])
        assignments.extend(routed);prior.extend(block)
    used={t for a in assignments for t in [a['primary_track_id'],*a['context_track_ids']]}
    return {'assignments':assignments,'tracks':[t for t in cards if t['track_id'] in used],
            'track_state_updates':cards,'next_track_ordinal':ordinal,'_public_normalized':True}


def source_key(ref):return (ref['source_system'],ref['session_id'],ref['message_id'])


def candidates(database,track_ids):
    result=[]
    with Store(database,read_only=True) as store:
        for track in track_ids:
            for row in store.conn.execute("SELECT e.* FROM pipeline_track_events p JOIN fact_events e ON e.item_id=p.event_id WHERE p.track_id=? AND e.status='active'",(track,)):
                refs=[dict(ref) for ref in store.conn.execute('SELECT * FROM fact_event_sources WHERE item_id=? ORDER BY id',(row['item_id'],))]
                originals=[]
                for ref in refs:
                    raw=store.conn.execute('SELECT * FROM raw_events WHERE source=? AND session_id=? AND source_event_id=?',source_key(ref)).fetchone()
                    originals.append(message(raw) if raw else message({'id':-int(digest(encode(source_key(ref)))[:12],16),'source':ref['source_system'],'source_event_id':ref['message_id'],'session_id':ref['session_id'],'role':ref['role'],'text':ref['content'],'created_at':ref['created_at']}))
                detail=store.conn.execute('SELECT details_json FROM pipeline_event_details WHERE event_id=?',(row['item_id'],)).fetchone()
                details=json.loads(detail[0]) if detail else {}
                blockers=reference_blockers(store.conn,row['item_id'])
                result.append({**dict(row),'event_id':row['item_id'],'primary_track_id':track,'track_id':track,'source_refs':refs,'originals':originals,
                    'source_message_ids':[m['id'] for m in originals],'session_ids':list({m['session_id'] for m in originals}),
                    'source_activity_roles':details.get('source_activity_roles',{}),'blocked':bool(blockers),'blocking_reasons':blockers})
    return result


def components(database,data,routed):
    assignments,tracks,_=route_result(data,routed)
    memberships,edges=routing_units(data['routing_messages'],assignments)
    by_id={row['id']:row for row in data['routing_messages']};stable_ids={m['id'] for m in data['messages']}
    graph={t['track_id']:set() for t in tracks}
    for a in assignments:
        for other in a['context_track_ids']:
            graph[a['primary_track_id']].add(other);graph[other].add(a['primary_track_id'])
    seen=set();result=[]
    for track in graph:
        if track in seen:continue
        group=set();pending=[track]
        while pending:
            key=pending.pop()
            if key in group:continue
            group.add(key);pending.extend(graph[key]-group)
        seen.update(group)
        own=[a for a in assignments if a['primary_track_id'] in group]
        stable=[by_id[a['source_message_id']] for a in own if a['source_message_id'] in stable_ids]
        if not stable:continue
        bases=candidates(database,group)
        context={a['source_message_id']:by_id[a['source_message_id']] for a in own}
        for base in bases:context.update({m['id']:m for m in base['originals']})
        result.append({'component_id':'+'.join(sorted(group)),'track_ids':sorted(group),'track_cards':[t for t in tracks if t['track_id'] in group],
            'messages':stable,'context_messages':list(context.values()),'parked_context_source_ids':[a['source_message_id'] for a in own if a['source_message_id'] not in stable_ids],
            'memberships':[unit for unit in memberships if unit['track_id'] in group],
            'context_edges':[edge for edge in edges if edge['unit_root_message_id'] in {unit['unit_root_message_id'] for unit in memberships if unit['track_id'] in group}],
            'base_event_candidates':bases,'context_session_ids':list({m['session_id'] for m in context.values()})})
    return result


def routing_units(messages,assignments):
    by_id={item['source_message_id']:item for item in assignments};units=[];edges=[]
    for item in messages:
        route=by_id[item['id']]
        units.append({'unit_root_message_id':item['id'],'source_message_ids':[item['id']],
                      'track_id':route['primary_track_id'],'session_id':item['session_id'],
                      'routing_role':route['routing_role']})
        edges.extend({'unit_root_message_id':item['id'],'track_id':track,'relation':'bridge'} for track in route['context_track_ids'])
    return units,edges


def writer_images(messages,owned_ids):
    images=[];missing=[]
    for m in messages:
        attachments=m['metadata'].get('attachments') or []
        candidates=[]
        for a in attachments:
            if a.get('kind')=='image' or str(a.get('mime_type','')).startswith('image/'):
                url=a.get('url') or a.get('image_url')
                if isinstance(url,dict):url=url.get('url')
                if not url and a.get('content_base64'):url='data:'+a.get('mime_type','image/png')+';base64,'+a['content_base64']
                candidates.append(url)
        original=m['metadata'].get('original_message') or {}
        if isinstance(original.get('content'),list):
            for part in original['content']:
                if isinstance(part,dict) and part.get('type')=='image_url':candidates.append(part.get('image_url',{}).get('url'))
        candidates.extend(re.findall(r'!\[[^\]]*\]\(<?([^\s)>]+)>?\)',m['content']))
        for position,url in enumerate(dict.fromkeys(candidates),1):
            if not isinstance(url,str) or not url.startswith(('https://','http://','data:image/')):missing.append(m['id']);continue
            images.append({'source_message_id':m['id'],'position':position,'evidence_role':'owned' if m['id'] in owned_ids else 'context_only','url':url})
    return images,list(dict.fromkeys(missing))


def request_for(database,batch,role,**fields):
    if role not in ROLES:raise ValueError('This pipeline stage is retired or unknown; request the next task')
    data=json.loads(batch['input_json']);config=snapshot(database,batch['id']);names=config['identity']
    request={'role':role,'identity':names,'batch_id':batch['id'],'contract':CONTRACT,**fields}
    model=config['models'].get(role)
    request['execution']={'mode':config['policy']['execution_mode'],'revision':config['revision'],
                          'model':model.get('model','') if model else ''}
    with latest.identity_scope(names):
        request['rules']=latest.materialize_agent_rules(role)
        if role=='track_router':
            request.update(messages=data['routing_messages'],active_tracks=track_state.parked(data['tracks']),
                           next_track_ordinal=data.get('next_track_ordinal',track_state.next_ordinal(data['scope'],data['tracks'])))
            prompt=latest.build_event_track_message_prompt(data['day'],request['messages'],request['active_tracks'],recent_context_messages=data['recent'])
        elif role=='event_curator':
            component=fields['component']
            images,missing=writer_images(component['context_messages'],{m['id'] for m in component['messages']})
            if missing:raise ValueError('绑定图片缺少原图，请补齐附件；原话仍保留')
            component['images']=freeze_images(images,component.get('images',[]))
            request['images']=[{**item,'evidence_role':'stable' if item['source_message_id'] in {m['id'] for m in component['messages']} else 'context_only'} for item in component['images']]
            prompt=latest.build_event_track_curator_prompt(data['day'],component)
            if request['images']:
                prompt+='\n必须逐张转录图片里的可见原文，标题、正文、评论按区块保留；看不清标 unreadable，不猜补。最终 JSON 额外包含 image_transcriptions 数组，每图恰好一项：'+encode({'input_image':1,'text':'可见原文','unreadable':False})
        else:
            event=fields['event'];component=fields['component']
            prompt=latest.build_event_writer_prompt(data['day'],'',fields['messages'],context_messages=component['context_messages'],
                track_cards=component['track_cards'],source_activity_roles={int(b['source_message_id']):b['activity_role'] for b in event['source_bindings']},
                previous_events=[b for b in component['base_event_candidates'] if b['event_id'] in event['base_event_ids']],
                track_context_events=component['base_event_candidates'])
            owned={m['id'] for m in fields['messages']};allowed={m['id'] for m in component['context_messages']}
            request['images']=[{**item,'evidence_role':'owned' if item['source_message_id'] in owned else 'context_only'} for item in component.get('images',[]) if item['source_message_id'] in allowed]
            request['curator_image_transcriptions']=[{**item,'evidence_role':'owned' if item['source_message_id'] in owned else 'context_only'} for item in component.get('curator_image_transcriptions',[]) if item['source_message_id'] in allowed]
            prompt+='\n<curator_image_transcriptions>\n'+encode(request['curator_image_transcriptions'])+'\n</curator_image_transcriptions>'
            prompt+='\n原图转录是附件原文，不是发送者新说的话。上下文图片不扩大归属。缺少指代时可且仅可返回 context_request，字段与 Curator 相同：'+encode({'context_request':{'track_id':component['track_ids'][0],'before_message_id':min(m['id'] for m in component['messages']),'reason':'missing_subject'}})
        if request.get('images'):
            prompt+='\n<image_inputs>\n'+encode([{**{k:v for k,v in item.items() if k not in ('url','original_url')},'input_image':i} for i,item in enumerate(request['images'],1)])+'\n</image_inputs>'
        if request.get('transcription_only'):
            prompt='只逐张转录实际附图的可见文字（标题、正文、评论），不切分事件、不决定归属。仅返回 {"image_transcriptions":[{"input_image":1,"text":"原文","unreadable":false}]}；每图一项，看不清标记，不猜补。'
        prompt=re.sub(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+','[原图见图像输入]',prompt)
        request['prompt']=prompt
    return request


def validate(request,output):
    if not isinstance(output,dict):raise ValueError('Stage output must be a JSON object')
    role=request['role']
    if role not in ROLES:raise ValueError('This pipeline stage is retired or unknown; request the next task')
    if request.get('transcription_only'):
        if set(output)!={'image_transcriptions'}:raise ValueError('补读图片只允许返回转录，不改变 Event 归属')
        bind_transcriptions(output,request.get('images',[]));return
    if role in ('event_curator','event_writer') and 'context_request' in output:
        c=output['context_request'];component=request['component']
        if request.get('context_read') or set(output)!={'context_request'} or not isinstance(c,dict) or set(c)!={'track_id','before_message_id','reason'} or c.get('track_id') not in component['track_ids'] or c.get('before_message_id')!=min(m['id'] for m in component['messages']) or c.get('reason') not in ('missing_subject','missing_origin','missing_prior_claim'):
            raise ValueError('Only one bounded component context request is allowed')
        return
    with latest.identity_scope(request['identity']):
        if role=='track_router':
            assignments,_,_=normalize_event_track_message_output(output,request['messages'],request['active_tracks'],session_id='validate',next_track_ordinal=1)
            routing_units(request['messages'],assignments)
        elif role=='event_curator':
            if 'context_request' in output:
                c=output['context_request'];component=request['component']
                if request.get('context_read') or set(output)!={'context_request'} or not isinstance(c,dict) or c.get('track_id') not in component['track_ids'] or c.get('before_message_id')!=min(m['id'] for m in component['messages']) or c.get('reason') not in ('missing_subject','missing_origin','missing_prior_claim'):
                    raise ValueError('Only one bounded component context request is allowed')
            else:
                bind_transcriptions(output,request.get('images',[]))
                latest.normalize_event_curator_output(decision(output),request['component'])
        else:
            errors=latest.validate_event_writer_result(output)
            if errors:raise ValueError('; '.join(errors))


def record_attempt(database,job_id,output,error=''):
    if not isinstance(output,str):output=encode(output)
    with Store(database) as store,store.transaction(immediate=True):
        number=store.conn.execute('SELECT count(*) FROM pipeline_attempts WHERE job_id=?',(job_id,)).fetchone()[0]+1
        store.conn.execute('INSERT INTO pipeline_attempts(job_id,attempt,created_at,output_text,error) VALUES (?,?,?,?,?)',
            (job_id,number,now(),output[:2_000_000],error[:1000]))


def submit(database,job_id,output):
    try:return _submit(database,job_id,output)
    except ValueError as error:
        record_attempt(database,job_id,encode(output),str(error))
        raise


def _submit(database,job_id,output):
    initialize(database)
    with Store(database) as store,store.transaction(immediate=True):
        row=store.conn.execute('SELECT j.*,b.status FROM pipeline_jobs j JOIN pipeline_batches b ON b.id=j.batch_id WHERE j.id=?',(job_id,)).fetchone()
        if row is None:raise ValueError('Unknown pipeline job')
        if json.loads(row['request_json'])['role'] not in ROLES:
            raise ValueError('This pipeline stage is retired; request the next task')
        if row['status'].startswith('superseded_'):raise ValueError('任务输入已更新，请重新领取任务；原话和已完成的归线仍保留')
        if row['output_json']:
            if row['output_json']!=encode(output):raise Conflict('This job already has a different result')
            return {'status':'unchanged','job_id':job_id}
        validate(json.loads(row['request_json']),output)
        store.conn.execute('UPDATE pipeline_jobs SET output_json=? WHERE id=?',(encode(output),job_id))
    return {'status':'accepted','job_id':job_id}


class AwaitAgent(Exception):
    def __init__(self,task):self.task=task


async def job(database,batch,request,key,runner):
    from ..work_tasks import progress
    identifier=batch['id']+':'+key
    with Store(database) as store,store.transaction(immediate=True):
        store.conn.execute('INSERT OR IGNORE INTO pipeline_jobs(id,batch_id,role,request_json) VALUES (?,?,?,?)',(identifier,batch['id'],key,encode(request)))
        row=store.conn.execute('SELECT * FROM pipeline_jobs WHERE id=?',(identifier,)).fetchone()
    incoming=request
    current_execution=request['execution']
    request=json.loads(row['request_json'])
    request['execution']=current_execution
    incoming.clear();incoming.update(request)
    if not row['output_json']:
        with Store(database) as store:store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',(encode(request),identifier))
    with Store(database,read_only=True) as store:
        completed=store.conn.execute("SELECT count(*) FROM pipeline_jobs WHERE batch_id=? AND output_json IS NOT NULL AND json_extract(request_json,'$.role')!='event_evidence'",(batch['id'],)).fetchone()[0]
        total=store.conn.execute("SELECT count(*) FROM pipeline_jobs WHERE batch_id=? AND json_extract(request_json,'$.role')!='event_evidence'",(batch['id'],)).fetchone()[0]
    progress(stage=request['role'],batch_id=batch['id'],completed=completed,total=total,job_id=identifier)
    if row['output_json']:return json.loads(row['output_json'])
    config=snapshot(database,batch['id']);policy=config['policy']
    prompt_chars=len(request['prompt'])+len(request['rules'])
    progress(prompt_chars=prompt_chars,timeout_seconds=policy['timeout_seconds'])
    if prompt_chars>policy['max_prompt_chars']:
        raise ValueError(f"当前 {request['role']} 提示词共 {prompt_chars} 字符，超过 {policy['max_prompt_chars']} 字符上限；请减小每批输入或调整提示词上限。原话未截断，已完成阶段保留。")
    if request.get('missing_images'):raise ValueError('绑定图片缺少可读取的原图，请补齐图片材料后重建任务；原话仍保留')
    verify_images(request.get('images',[]))
    model=config['models'].get(request['role']) if policy['execution_mode']!='agent' else None
    if not runner and not model:
        raise AwaitAgent({'status':'awaiting_agent','job_id':identifier,'role':request['role'],'request':request,
            'instructions':'Configure an MCP agent as described in Settings > Agent guide, read the frozen prompt, submit with pipeline_submit and call pipeline_next again.'})
    if runner:output=await runner(request['role'],request)
    else:
        from ..model_runtime import complete
        prompt=request['prompt']
        for attempt in range(3):
            progress(attempt=attempt+1,stage=request['role'])
            raw='';received=False
            try:
                content=([{'type':'text','text':prompt}]+[{'type':'image_url','image_url':{'url':item['url']}} for item in request.get('images',[])]) if request.get('images') else prompt
                response=await asyncio.wait_for(complete({**model,'request_timeout_seconds':policy['timeout_seconds']},
                    {'messages':[{'role':'system','content':request['rules']},{'role':'user','content':content}],
                     'response_format':{'type':'json_object'},'max_tokens':8192}),timeout=policy['timeout_seconds']+20)
                received=True
                raw=response['choices'][0]['message']['content']
                output=json.loads(raw)
                validate(request,output)
                record_attempt(database,identifier,raw)
                break
            except Exception as error:
                from ..work_tasks import failure_reason
                reason=failure_reason(error)
                record_attempt(database,identifier,raw,reason)
                progress(error=reason,attempt=attempt+1)
                if not received or not isinstance(error,ValueError) or attempt==2:raise
                correction='\n请按原角色规则纠正结构或证据校验错误，只返回完整 JSON。保留人物归属、比喻及不确定程度，不按词句数量改写文风。编号使用原始编号，不得按展示位置重新编号。\n'+encode({'validation_error':reason,'allowed_ids':allowed_ids(request)})
                room=policy['max_prompt_chars']-len(request['rules'])-len(request['prompt'])-len(correction)-80
                if room<0:raise ValueError('提示词上限不足以容纳纠错请求，请减小每批输入。') from error
                prompt=request['prompt']+correction+'\n上一份不合格输出（仅用于纠错，可能截断）：\n'+raw[:min(room,10000)]
        progress(error='')
    submit(database,identifier,output)
    progress(completed=completed+1)
    return output


def extend_context(database,component,context_request):
    # Read at most six prior routed ownership units from the declared Track.
    with Store(database,read_only=True) as store:
        rows=store.conn.execute('SELECT r.* FROM pipeline_routes p JOIN raw_events r ON r.id=p.raw_id WHERE r.id<? AND (json_extract(p.route_json,\'$.primary_track_id\')=? OR EXISTS (SELECT 1 FROM json_each(p.route_json,\'$.context_track_ids\') WHERE value=?)) ORDER BY r.id DESC',
            (context_request['before_message_id'],context_request['track_id'],context_request['track_id'])).fetchall()
    existing={m['id']:m for m in component['context_messages']}
    prior=[message(r) for r in reversed(rows) if message(r)['session_id'] in component['context_session_ids']]
    units=[[item] for item in sorted(prior,key=lambda item:item['id'])[-6:]]
    selected=[m for unit in units for m in unit]
    existing.update({m['id']:m for m in selected})
    covered={source for unit in component['memberships'] for source in unit['source_message_ids']}
    extra=[{'unit_root_message_id':unit[0]['id'],'source_message_ids':[m['id'] for m in unit if m['id'] not in covered],'track_id':context_request['track_id'],'session_id':unit[0]['session_id'],'routing_role':'primary_activity'} for unit in units if all(m['id'] not in covered for m in unit)]
    return {**component,'context_messages':list(existing.values()),'memberships':component['memberships']+extra,'context_receipt':{'read_source_ids':[m['id'] for m in selected]}}


def settle(database,batch,data,routed,plans):
    arc_linking_enabled=bool(read_settings(database)['assignments'].get('arc_linker'))
    if arc_linking_enabled:
        from ..arc_linking import initialize as initialize_arc_linking
        initialize_arc_linking(database)
    items=[];details=[];processed={};all_new={m['id'] for m in data['messages']}
    for component,plan,event_results in plans:
        processed.update({key:'skipped' for key in plan['skip_source_message_ids']})
        for event,written in event_results:
            if not written['evidence_sufficient']:continue
            by_id={m['id']:m for m in component['context_messages']}
            refs=[]
            for key in event['source_message_ids']:
                m=by_id[key]
                refs.append({'source_system':m['source'],'session_id':m['original_session_id'],'message_id':m.get('source_event_id') or str(key),'role':m['role'],'created_at':m['created_at'],'content':m['content'],'binding_method':'archive_pipeline','evidence_kind':'primary' if not refs else 'supporting'})
            refs=list({source_key(ref):ref for ref in refs}.values())
            item={'type':'event','title':written['title'],'body':written['event_draft'],'recallable':written['recallable'],'source_refs':refs,
                'origin_id':'assistant_bridge:'+batch['id']+':'+str(len(items))}
            bases=[b for b in component['base_event_candidates'] if b['event_id'] in event['base_event_ids']]
            if bases:
                item.update(supersedes_item_ids=[b['event_id'] for b in bases],expected_predecessors=[{'item_id':b['event_id'],'fingerprint':b['fingerprint'],
                    'source_keys':[dict(zip(('source_system','session_id','message_id'),source_key(ref))) for ref in b['source_refs']]} for b in bases])
            items.append(item);details.append({'track_id':event['primary_track_id'],'writer':written,'curator_image_transcriptions':written.get('curator_image_transcriptions',[]),'source_activity_roles':{str(b['source_message_id']):b['activity_role'] for b in event['source_bindings']}})
            processed.update({key:'settled' for key in event['source_message_ids'] if key in all_new})
    assignments,tracks,_=route_result(data,routed)
    result={'status':'processed','batch_id':batch['id'],'completed_at':now(),'events':len(items),'processed_originals':len(processed),
            'pending':len(data['messages'])+len(data['parked'])-len(processed),
            'skipped':sum(value=='skipped' for value in processed.values()),
            'deferred':len({source for _,plan,_ in plans for source in plan['defer_source_message_ids']}),
            'protected_deferrals':[entry for _,plan,_ in plans for entry in plan['hard_skips']]}
    def finish(conn):
        with latest.identity_scope(identity(database)):
            cards=routed.get('track_state_updates') or track_state.update_cards(data['tracks'],assignments,tracks,data['routing_messages'],data['scope'])
        track_state.persist(conn,cards,data['scope'])
        for a in assignments:conn.execute('INSERT OR REPLACE INTO pipeline_routes VALUES (?,?)',(a['source_message_id'],encode(a)))
        for item,detail in zip(items,details):
            key=conn.execute('SELECT item_id FROM fact_events WHERE origin_id=?',(item['origin_id'],)).fetchone()[0]
            conn.execute('INSERT OR IGNORE INTO pipeline_track_events VALUES (?,?)',(detail['track_id'],key))
            conn.execute('INSERT OR REPLACE INTO pipeline_event_details VALUES (?,?)',(key,encode(detail)))
            if arc_linking_enabled:
                from ..arc_linking import enqueue
                fingerprint=conn.execute('SELECT fingerprint FROM fact_events WHERE item_id=?',(key,)).fetchone()[0]
                enqueue(conn,key,fingerprint)
        for key,outcome in processed.items():conn.execute('INSERT OR IGNORE INTO raw_processing VALUES (?,?,?)',(key,batch['id'],outcome))
        conn.execute("UPDATE pipeline_batches SET status='done',result_json=? WHERE id=?",(encode(result),batch['id']))
    if items:
        Events(database).settle(batch['id'],items,before_commit=finish)
    else:
        with Store(database) as store,store.transaction(immediate=True):finish(store.conn)
    return result


async def advance(database,*,include_recent=False,runner=None):
    from ..work_tasks import execute
    return await execute(database,'pipeline',lambda:_advance(database,include_recent=include_recent,runner=runner))


async def _advance(database,*,include_recent=False,runner=None):
    with execution(database):
        return await _advance_frozen(database,include_recent=include_recent,runner=runner)


async def _advance_frozen(database,*,include_recent=False,runner=None):
    initialize(database)
    batch=new_batch(database,include_recent)
    if not batch:return {'status':'current','note':'No stable dialogue units are ready.'}
    data=json.loads(batch['input_json']);plans=[]
    try:
        request=request_for(database,batch,'track_router')
        with Store(database,read_only=True) as store:
            cached=[store.conn.execute('SELECT route_json FROM pipeline_routes WHERE raw_id=?',(m['id'],)).fetchone() for m in data['routing_messages']]
        if cached and all(cached):
            routes=[json.loads(row[0]) for row in cached]
            used={t for a in routes for t in [a['primary_track_id'],*a['context_track_ids']]}
            routed={'message_assignments':[{'source_message_id':a['source_message_id'],'primary_track_ref':a['primary_track_id'],'context_track_refs':a['context_track_ids'],'routing_role':a['routing_role']} for a in routes],
                'track_updates':[{'track_ref':t['track_id'],**{k:t[k] for k in ('subject','throughline','event_policy','status')}} for t in data['tracks'] if t['track_id'] in used]}
        else:routed=await route_batch(database,batch,data,runner)
        if 'components' not in data:
            data['components']=components(database,data,routed)
            with Store(database) as store:store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch['id']))
        for index,component in enumerate(data['components']):
            request=request_for(database,batch,'event_curator',component=component)
            with Store(database) as store:store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch['id']))
            output=await job(database,batch,request,f'event_curator:{index}',runner)
            component=request['component']
            if 'context_request' in output:
                component=extend_context(database,component,output['context_request'])
                request=request_for(database,batch,'event_curator',component=component,context_read=True)
                output=await job(database,batch,request,f'event_curator:{index}:context',runner)
                component=request['component']
            component['curator_image_transcriptions']=bind_transcriptions(output,request.get('images',[]))
            plan=latest.normalize_event_curator_output(decision(output),component);event_results=[]
            by_id={m['id']:m for m in component['context_messages']}
            for ordinal,event in enumerate(plan['events']):
                owned=[by_id[key] for key in event['source_message_ids']]
                request=request_for(database,batch,'event_writer',messages=owned,event=event,component=component)
                written=await job(database,batch,request,f'event_writer:{index}:{ordinal}',runner)
                if 'context_request' in written:
                    reading=extend_context(database,component,written['context_request'])
                    image_task=request_for(database,batch,'event_curator',component=reading,transcription_only=True)
                    if image_task.get('images'):
                        transcription=await job(database,batch,image_task,f'writer_context_images:{index}:{ordinal}',runner)
                        reading=image_task['component']
                        reading['curator_image_transcriptions']=bind_transcriptions(transcription,image_task['images'])
                    request=request_for(database,batch,'event_writer',messages=owned,event=event,component=reading,context_read=True)
                    written=await job(database,batch,request,f'event_writer:{index}:{ordinal}:context',runner)
                written={key:value for key,value in written.items() if key!='result_or_unfinished'}
                written['curator_image_transcriptions']=request.get('curator_image_transcriptions',[])
                event_results.append((event,written))
            plans.append((component,plan,event_results))
        return settle(database,batch,data,routed,plans)
    except AwaitAgent as wait:return wait.task


def tools_for(settings):
    async def pipeline_next(include_recent:bool=False)->dict:
        """Advance Router -> Curator (with image transcription) -> Writer; return a frozen agent task if its model is blank."""
        return await advance(settings.database,include_recent=include_recent)
    def pipeline_submit(job_id:str,output:dict)->dict:
        """Submit a frozen role result. Curator owns boundaries; Writer never changes them."""
        return submit(settings.database,job_id,output)
    return {'pipeline_next':pipeline_next,'pipeline_submit':pipeline_submit}

async def flush_routes(database):
    with execution(database):return await _flush_routes_frozen(database)


async def _flush_routes_frozen(database):
    """Daytime routing: five completed envelopes and twenty-minute silence; no Events."""
    config=snapshot(database,'routing')
    if config['policy']['execution_mode']=='agent' or not config['models']['track_router']:return
    initialize(database)
    with Store(database,read_only=True) as store:
        upload=''
        if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='file_imports'").fetchone():
            upload=" AND (json_extract(r.metadata_json,'$.import_upload_id') IS NULL OR json_extract(r.metadata_json,'$.import_upload_id') IN (SELECT id FROM file_imports WHERE cursor=json_array_length(payload_json,'$.entries')))"
        rows=[message(r) for r in store.conn.execute("SELECT r.* FROM raw_events r WHERE NOT EXISTS (SELECT 1 FROM pipeline_routes p WHERE p.raw_id=r.id) AND NOT EXISTS (SELECT 1 FROM raw_processing p WHERE p.raw_id=r.id)"+upload+' ORDER BY r.id')]
    sessions={}
    for row in rows:sessions.setdefault((row['source'],row['original_session_id']),[]).append(row)
    current=datetime.now(timezone.utc)
    for (source,session),messages in sessions.items():
        units=flushable_dialogue_units(messages,now=current)
        if len(units)<5:continue
        messages=[row for unit in units for row in unit];scope=digest(encode([source,session]))[:20]
        with Store(database) as store:
            with latest.identity_scope(identity(database)):
                tracks,ordinal=track_state.load_tracks(store,source,session,messages[0]['id'],message)
            recent=[message(r) for r in store.conn.execute('SELECT * FROM raw_events WHERE source=? AND session_id=? AND id<? ORDER BY id DESC LIMIT 6',(source,session,messages[0]['id']))][::-1]
            data={'contract':CONTRACT,'routing_messages':messages,'tracks':tracks,'next_track_ordinal':ordinal,'scope':scope,'recent':recent,'day':current.astimezone(TZ).date().isoformat()}
            key='route:'+digest(encode(data));batch={'id':key,'input_json':encode(data)}
            store.conn.execute("INSERT OR IGNORE INTO pipeline_batches(id,scope,input_json,status) VALUES (?,?,?,'routing_only')",(key,scope,batch['input_json']))
        output=await route_batch(database,batch,data,None)
        assignments,updates,_=route_result(data,output)
        with Store(database) as store,store.transaction(immediate=True):
            track_state.persist(store.conn,output['track_state_updates'],scope)
            for a in assignments:store.conn.execute('INSERT OR REPLACE INTO pipeline_routes VALUES (?,?)',(a['source_message_id'],encode(a)))
            store.conn.execute("UPDATE pipeline_batches SET status='routed' WHERE id=?",(key,))


async def scheduled_advance(database):
    if not read_settings(database)['pipeline']['auto_enabled']:
        return {'status':'auto_paused'}
    from ..work_tasks import execute
    return await execute(database,'pipeline',lambda:_scheduled_advance(database))


async def _scheduled_advance(database):
    if not read_settings(database)['pipeline']['auto_enabled']:
        return {'status':'auto_paused'}
    initialize(database)
    await flush_routes(database)
    current=datetime.now(TZ);day=current.date().isoformat()
    if current.hour<3:return {'status':'waiting_settlement_window'}
    with Store(database,read_only=True) as store:
        row=store.conn.execute('SELECT completed FROM pipeline_schedule WHERE day=?',(day,)).fetchone()
    if row and row[0]:return {'status':'settled_today'}
    result=await _advance(database)
    if result['status']=='current':
        with Store(database) as store:store.conn.execute('INSERT OR REPLACE INTO pipeline_schedule VALUES (?,1)',(day,))
    return result
