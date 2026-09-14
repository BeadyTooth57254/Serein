import test from 'node:test';
import assert from 'node:assert/strict';
import {memoDateParts, changeMemoDate} from '../src/utils/memoDate.js';

test('date-only and empty reminders keep their whole-day semantics', () => {
  assert.deepEqual(memoDateParts('2026-10-01'), {date:'2026-10-01',time:''});
  assert.equal(changeMemoDate('', 'date', '2026-10-01'), '2026-10-01');
  assert.equal(changeMemoDate('2026-10-01T09:30:00+08:00', 'time', ''), '2026-10-01');
  assert.equal(changeMemoDate('2026-10-01T09:30:00+08:00', 'date', ''), '');
});
test('picker preserves the scheduled instant across timezone and date boundaries', () => {
  assert.deepEqual(memoDateParts('2026-09-30T18:45:00Z'), {date:'2026-10-01',time:'02:45'});
  assert.deepEqual(memoDateParts('2026-10-01T09:30:00'), {date:'2026-10-01',time:'09:30'});
  assert.equal(changeMemoDate('2026-09-30T18:45:00Z', 'date', '2026-10-02'), '2026-10-02T02:45:00+08:00');
  assert.equal(changeMemoDate('2026-10-01', 'time', '09:30'), '2026-10-01T09:30:00+08:00');
});
