import {test} from 'node:test';
import assert from 'node:assert/strict';
import {upstreamsForSave} from '../src/upstreamSecrets.js';

function formWith(values) {
  return {
    querySelectorAll() {
      return Object.entries(values).map(([upstreamId,value])=>({dataset:{upstreamId},value}));
    },
  };
}

test('the password field value replaces a configured upstream key even without a state change',()=>{
  const draft=[{id:'provider',name:'Provider',api_key:'',api_key_configured:true,clear_key:false}];
  const [saved]=upstreamsForSave(draft,formWith({provider:'synthetic-new-key'}));
  assert.equal(saved.api_key,'synthetic-new-key');
  assert.equal('api_key_configured' in saved,false);
  assert.equal('clear_key' in saved,false);
  assert.equal(draft[0].api_key,'');
});

test('a non-empty password field wins over a stale clear-key choice',()=>{
  const [saved]=upstreamsForSave(
    [{id:'provider',api_key_configured:true,clear_key:true,api_key_env:'PROVIDER_KEY'}],
    formWith({provider:'synthetic-replacement'}),
  );
  assert.equal(saved.api_key,'synthetic-replacement');
  assert.equal(saved.api_key_env,'PROVIDER_KEY');
});

test('an empty password preserves a configured key unless removal is explicit',()=>{
  const upstream={id:'provider',api_key:'',api_key_configured:true,api_key_env:'PROVIDER_KEY'};
  const [preserved]=upstreamsForSave([{...upstream,clear_key:false}],formWith({provider:''}));
  assert.equal('api_key' in preserved,false);
  assert.equal(preserved.api_key_env,'PROVIDER_KEY');
  const [removed]=upstreamsForSave([{...upstream,clear_key:true}],formWith({provider:''}));
  assert.equal(removed.api_key,'');
  assert.equal(removed.api_key_env,'');
});

test('saving the configuration tab falls back to the current draft without a model form',()=>{
  const [saved]=upstreamsForSave([{id:'provider',api_key:'typed-key'}],null);
  assert.equal(saved.api_key,'typed-key');
});
