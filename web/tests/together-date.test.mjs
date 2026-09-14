import {test} from 'node:test';
import assert from 'node:assert/strict';
import {localCalendarDate, togetherDays, togetherCaption} from '../src/storage/togetherDate.js';

test('meeting day is day one and calendar days cross months and leap days', () => {
  assert.equal(togetherDays('2026-09-10', '2026-09-10'), 1);
  assert.equal(togetherCaption('2026-09-09', '2026-09-10'), '在一起的 2 天');
  assert.equal(togetherDays('2024-02-28', '2024-03-01'), 3);
  assert.equal(togetherDays('2025-12-31', '2026-01-01'), 2);
  assert.equal(togetherDays('2026-03-08', '2026-03-09'), 2);
  assert.equal(localCalendarDate(new Date(2026, 8, 10, 0, 1)), '2026-09-10');
});

test('empty, invalid and future dates do not produce a misleading count', () => {
  for (const date of ['', '2026-02-30', '2026-09-11', 'not-a-date']) {
    assert.equal(togetherDays(date, '2026-09-10'), null);
    assert.equal(togetherCaption(date, '2026-09-10'), '从这里开始');
  }
});
