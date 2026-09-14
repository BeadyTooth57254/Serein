import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bookAppearance, chronologicalSources, shortBookTitle } from '../src/storage/narrativeAppearance.js';
import { buildNarrativeTaskPrompt } from '../server/narrativeCodexRunner.mjs';

test('cover families vary on the shelf while dark stays reserved for more than 15 sources', () => {
  for (const count of [0, 5]) assert.equal(bookAppearance(count)['--book-cover'], '#fdfdfb');
  for (const count of [6, 15]) assert.equal(bookAppearance(count)['--book-cover'], '#dbe2df');
  for (const count of [16, 100]) assert.equal(bookAppearance(count)['--book-cover'], '#596660');
  assert.equal(new Set(Array.from({ length: 101 }, (_, n) => bookAppearance(n)['--book-cover'])).size, 3);
  assert.equal(bookAppearance(15)['--book-ink'], '#293832');
  assert.equal(bookAppearance(16)['--book-ink'], '#f7faf8');
  for (const count of [0, 5, 6, 15]) {
    const covers = [0, 1, 2].map(position => bookAppearance(count, position)['--book-cover']);
    assert.equal(new Set(covers).size, 3);
    assert.ok(!covers.includes('#596660'));
  }
  for (const position of [0, 1, 2, 29]) assert.equal(bookAppearance(16, position)['--book-cover'], '#596660');
  assert.equal(Array.from(shortBookTitle('🌧'.repeat(20))).length, 17);
});

test('sources sort across types by actual time, without mutating the input', () => {
  const sources = [{ id: 'scene', date: '2026-01-01' }, { id: 'unknown' }, { id: 'diary', date: '2025-01-01' }];
  assert.deepEqual(chronologicalSources(sources).map(s => s.id), ['diary', 'scene', 'unknown']);
  assert.equal(sources[0].id, 'scene');
});

test('writer receives the user theme independently of the short book title', () => {
  const prompt = buildNarrativeTaskPrompt({ mode: 'rewrite', title: '雨声', writingFocus: '一起听雨的日子', materials: {}, roleRules: 'rules' });
  assert.ok(prompt.includes('"writing_focus":"一起听雨的日子"'));
});
