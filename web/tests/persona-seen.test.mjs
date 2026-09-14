import test from 'node:test';
import assert from 'node:assert/strict';
import { personaChanges, readPersonaSeen, writePersonaSeen } from '../src/utils/personaSeen.js';

const storage = () => {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
};
test('first visit establishes history without replay; an empty first visit still sees its first new event', () => {
  assert.deepEqual(personaChanges([{ id: 8 }], null), { latest: 8, baseline: true, unread: [] });
  assert.equal(personaChanges([], null).latest, 0);
  assert.equal(personaChanges([{ id: 1 }], 0).unread.length, 1);
});
test('unseen changes survive leaving and only acknowledgment consumes them', () => {
  const store = storage();
  writePersonaSeen('returning', 8, store);
  const events = [{ id: 11, error: 'bad JSON' }, { id: 10 }, { id: 9 }, { id: 8 }];
  for (let visit = 0; visit < 2; visit++) {
    assert.equal(personaChanges(events, readPersonaSeen('returning', store)).unread.length, 2);
  }
  writePersonaSeen('returning', 10, store);
  assert.deepEqual(personaChanges(events, readPersonaSeen('returning', store)).unread, []);
  assert.equal(personaChanges([{ id: 12 }, ...events], readPersonaSeen('returning', store)).unread.length, 1);
});
test('window markers are independent; errors do not rain; a replaced database establishes a new baseline', () => {
  const store = storage();
  writePersonaSeen('window-a', 80, store);
  assert.equal(readPersonaSeen('window-b', store), null);
  assert.equal(personaChanges([{ id: 2 }], readPersonaSeen('window-a', store)).baseline, true);
  assert.deepEqual(personaChanges([{ id: 81, error: 'bad JSON' }], 80).unread, []);
});
