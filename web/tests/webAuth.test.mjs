import test from 'node:test';
import assert from 'node:assert/strict';
import { scryptSync } from 'node:crypto';
import { authenticate } from '../server/webAuth.mjs';

test('web credentials require both username and the correct password', () => {
  const salt = Buffer.from('00112233445566778899aabbccddeeff', 'hex');
  const record = { username: '测试', salt: salt.toString('hex'), hash: scryptSync('a:long-password', salt, 32).toString('hex') };
  const header = text => 'Basic ' + Buffer.from(text).toString('base64');
  assert.equal(authenticate(header('测试:a:long-password'), record), true);
  assert.equal(authenticate(header('测试:wrong'), record), false);
  assert.equal(authenticate(header('other:a:long-password'), record), false);
  assert.equal(authenticate('Bearer anything', record), false);
});
