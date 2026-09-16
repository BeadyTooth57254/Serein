import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {callSereinBackend} from '../server/sereinBackend.mjs';
import {buildNarrativeTaskPrompt,maxNarrativeBodyChars,normalizeNarrativeWriterResult,runNarrativeCodexTask} from '../server/narrativeCodexRunner.mjs';
import {buildSceneEvidenceRefs} from '../server/sceneEvidenceBridge.mjs';
import {resolveGatewayObservationOutcome,gatewayRequestLabel,resolveBridgeObservationOutcome} from '../src/recallObservationOutcome.js';
import {readFileSync} from 'node:fs';

test('darkroom lock copy follows the configured AI name',()=>{
  const source=readFileSync(new URL('../src/data/diary.js',import.meta.url),'utf8');
  assert.match(source,/lockedTitle[\s\S]*identityName\("assistant"\)[\s\S]*锁了门/);
  assert.doesNotMatch(source,/Haven/);
});

test('observation distinguishes disabled recall, preparation, failure and successful injection',()=>{
  const prepared={observation_version:1,prepared_ids:['scene:a'],injected_bucket_ids:[],request_status:'upstream_pending'};
  assert.equal(resolveGatewayObservationOutcome(prepared,'injected'),'pending');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'failed'},'injected'),'failed');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'interrupted'},'injected'),'failed');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'completed',recall_state:'disabled'},'no_match'),'skip');
  assert.equal(gatewayRequestLabel({...prepared,request_status:'completed',recall_state:'disabled'}),'');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'completed',recall_state:'no_match'},'skip'),'no_match');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'completed',injected_bucket_ids:['scene:a']},'skip'),'injected');
  assert.equal(resolveGatewayObservationOutcome({},'injected'),'injected');
  assert.equal(gatewayRequestLabel({}),'历史记录');
  assert.equal(resolveBridgeObservationOutcome({hookOutcome:'no_match'}),'no_match');
  assert.match(gatewayRequestLabel({...prepared,recall_diagnostics:{reranker_error:'http_401'}}),/认证失败（401）/);
  assert.match(gatewayRequestLabel({...prepared,recall_diagnostics:{reason:'hook_deadline_before_reranker'}}),/时间预算不足/);
});

test('missing configuration never calls a remote fallback',async()=>{
  let called=false;
  await assert.rejects(callSereinBackend('/health',{}, {env:{},fetchImpl:()=>{called=true;}}),/not_configured/);
  assert.equal(called,false);
});
test('backend credentials stay in server request headers',async()=>{
  const result=await callSereinBackend('/v1/memories/test',{}, {
    env:{SEREIN_MEMORY_URL:'http://localhost:9000',SEREIN_MEMORY_TOKEN:'synthetic'},
    fetchImpl:async(url,options)=>{
      assert.equal(url,'http://localhost:9000/v1/memories/test');
      assert.equal(options.headers.Authorization,'Bearer synthetic');
      return {ok:true,status:200,json:async()=>({id:'test'})};
    }});
  assert.deepEqual(result.payload,{id:'test'});
});
test('writer preserves update and rewrite material boundaries',()=>{
  const args={title:'Book club',currentBody:'Previous body',materials:{items:[]},roleRules:'Use original sources'};
  assert.match(buildNarrativeTaskPrompt({...args,mode:'update'}),/Previous body/);
  assert.doesNotMatch(buildNarrativeTaskPrompt({...args,mode:'rewrite'}),/Previous body/);
  const focusedPrompt = buildNarrativeTaskPrompt({...args,mode:'rewrite',writingFocus:'Book'.repeat(200)});
  const focused = JSON.parse(focusedPrompt.split('<narrative_writer_input_json>')[1].split('</narrative_writer_input_json>')[0]);
  assert.equal(focused.writing_focus.length,500);
});
test('writer enforces a 500-character body and source-bound speech acts',()=>{
  const roleRules=readFileSync(new URL('../codex_agents/narrative_writer/AGENTS.md',import.meta.url),'utf8');
  assert.equal(maxNarrativeBodyChars,500);
  assert.match(roleRules,/Do not infer speech acts/);
  assert.match(roleRules,/no more than 500 characters/);
  const self_review={source_bound:true,final_supported_versions:true,no_correction_narration:true,material_relevance:true,
    no_new_inference:true,no_meta_explanation:true,no_forced_closure:true,dates_preserved:true,identity_correct:true};
  assert.equal(normalizeNarrativeWriterResult({evidence_sufficient:true,body:'x'.repeat(500),issues:[],self_review}).body.length,500);
  assert.throws(()=>normalizeNarrativeWriterResult({evidence_sufficient:true,body:'x'.repeat(501),issues:[],self_review}),/narrative_writer_body_too_long/);
});
test('writer disabled means no runner starts',async()=>{
  delete process.env.SEREIN_WRITER_ENABLED;
  await assert.rejects(runNarrativeCodexTask({}, {backend:async()=>({ok:true,payload:{identity:{user_name:"User",ai_name:"AI"},upstream:{writer_enabled:false}}})}),/disabled/);
});
test('main-model authoring blocks both API and external automatic Writer',async()=>{
  const previous=process.env.SEREIN_WRITER_ENABLED;
  process.env.SEREIN_WRITER_ENABLED='1';
  try {
    for(const writer_enabled of [false,true])await assert.rejects(runNarrativeCodexTask({}, {
      backend:async()=>({ok:true,payload:{identity:{},features:{narrative_tools:true},upstream:{writer_enabled}}}),
    }),/disabled_main_model_authoring/);
  } finally {
    if(previous===undefined)delete process.env.SEREIN_WRITER_ENABLED;else process.env.SEREIN_WRITER_ENABLED=previous;
  }
});

test('writer renders the saved identities without rewriting original materials',()=>{
  const prompt=buildNarrativeTaskPrompt({mode:'rewrite',title:'Reading plan',materials:{source:'Alex said the meeting was Monday.'},
    roleRules:'Write as {ai_name}; the user is {user_name}.',identity:{user_name:'Nori',ai_name:'Atlas'}});
  assert.match(prompt,/Write as Atlas; the user is Nori/);
  assert.match(prompt,/Alex said the meeting was Monday/);
  assert.doesNotMatch(prompt,/\{ai_name\}|\{user_name\}/);
});
test('evidence retains original host identities, not archive row IDs',()=>{
  const [ref]=buildSceneEvidenceRefs([{id:1,source_system:'sample-client',source_message_id:'message-99',
    session_id:'session-a',role:'user',created_at:'2025-01-01T00:00:00Z',content:'Original text'}],
    [{messageId:1,evidenceKind:'primary'}]);
  assert.equal(ref.message_id,'message-99');
  assert.equal(ref.source_system,'sample-client');
  assert.equal(ref.content,'Original text');
});

test('configured Writer receives images and blocks unavailable image evidence',async()=>{
  const review={source_bound:true,final_supported_versions:true,no_correction_narration:true,material_relevance:true,
    no_new_inference:true,no_meta_explanation:true,no_forced_closure:true,dates_preserved:true,identity_correct:true};
  const requests=[];
  const backend=async(path,options)=>{
    if(path==='/v1/settings')return {ok:true,payload:{identity:{user_name:'Nori',ai_name:'Atlas'},
      upstream:{writer_enabled:true},models:[{id:'writer',model:'synthetic-writer'}],assignments:{writer:'writer'}}};
    requests.push(options.body);
    return {ok:true,payload:{result:{evidence_sufficient:true,body:'The notice changed the room.',issues:[],self_review:review}}};
  };
  const args={mode:'rewrite',title:'Library notice',currentBody:'',writingFocus:'How the reading group grew',
    roleDir:fileURLToPath(new URL('../codex_agents/narrative_writer/',import.meta.url))};
  const result=await runNarrativeCodexTask({...args,materials:{content:'![notice](https://example.org/notice.png)'}},{backend});
  assert.equal(result.status,'ok');
  assert.deepEqual(requests[0].image_inputs,['https://example.org/notice.png']);
  assert.match(requests[0].prompt,/Atlas/);
  assert.match(requests[0].prompt,/How the reading group grew/);
  const unavailable=await runNarrativeCodexTask({...args,materials:{content:'![notice](local-notice.png)'}},{backend});
  assert.equal(unavailable.status,'insufficient');
  assert.equal(unavailable.body,'');
  assert.equal(requests.length,1);
});
