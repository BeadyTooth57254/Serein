import test from 'node:test';
import assert from 'node:assert/strict';
import { createObservationFeed } from '../src/storage/recallObservationFeed.js';

test('reaching the footer during a head refresh queues exactly one older page', async () => {
  let releaseRefresh;
  const calls = [];
  const feed = createObservationFeed({ onChange() {}, request: async (_, options) => {
    calls.push(options);
    if (calls.length === 1) return { items: [{ id: 30 }], has_more: true, next_before_id: 30 };
    if (options.afterId != null) return new Promise(resolve => { releaseRefresh = resolve; });
    return { items: [{ id: 29 }], has_more: true, next_before_id: 29 };
  } });
  try {
    await feed.refresh('gateway');
    const refreshing = feed.refresh('gateway');
    const firstFooter = feed.loadEarlier('gateway');
    const repeatedFooter = feed.loadEarlier('gateway');
    assert.equal(calls.length, 2);
    releaseRefresh({ items: [], next_after_id: 30 });
    await Promise.all([refreshing, firstFooter, repeatedFooter]);
    assert.equal(calls.length, 3);
    assert.equal(calls[2].beforeId, 30);
    assert.deepEqual(feed.snapshot().gateway.rows.map(row => row.id), [30, 29]);
    assert.equal(feed.snapshot().gateway.afterId, 30);
    assert.equal(feed.snapshot().gateway.nextBeforeId, 29);
  } finally { feed.dispose(); }
});
