import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {createUuid} from '../src/utils/createUuid.js';

test('uses native randomUUID when available, preserving its receiver', () => {
  const source = {randomUUID() { assert.equal(this, source); return 'native-id'; }};
  assert.equal(createUuid(source), 'native-id');
});

test('HTTP fallback generates distinct UUID v4 IDs without randomUUID', () => {
  const source = {getRandomValues: bytes => webcrypto.getRandomValues(bytes)};
  const ids = Array.from({length: 1000}, () => createUuid(source));
  for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(new Set(ids).size, ids.length);
});
