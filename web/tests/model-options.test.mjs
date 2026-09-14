import test from 'node:test';
import assert from 'node:assert/strict';
import {upstreamModels,taskModelOptions} from '../src/modelOptions.js';

test('model options retain IDs but display aliases and filter assigned auxiliary models',()=>{
  const models=upstreamModels([{name:'Provider',protocol:'openai',models:[
    {id:'opaque-chat',upstream_model:'actual-chat',label:'Chat'},
    {id:'opaque-embed',upstream_model:'actual-embed',label:''},
    {id:'opaque-rerank',upstream_model:'actual-rerank'},
  ]},{name:'Messages',protocol:'anthropic',models:['other-chat']}]);
  assert.deepEqual(models.map(m=>m.label),['Chat','actual-embed','actual-rerank','other-chat']);
  const assignments={embedding:'opaque-embed',reranker:'opaque-rerank'};
  for(const task of ['chat','writer','persona'])assert.deepEqual(taskModelOptions(models,assignments,task).map(m=>m.id),['opaque-chat','other-chat']);
  for(const task of ['embedding','reranker'])assert.deepEqual(taskModelOptions(models,assignments,task).map(m=>m.id),['opaque-chat','opaque-embed','opaque-rerank']);
  assert.equal(taskModelOptions(models,{embedding:'opaque-chat'},'writer').some(m=>m.id==='opaque-embed'),true);
});
