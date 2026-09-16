import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mergeMissionActivity } from '../.test-dist/mission-activity-merge.js';
const row = (deliveryId, seq, content = '', revision, status = 'running') => ({
  kind:'message', deliveryId, seq, content,
  ...(revision == null ? {} : {missionActivity: {revision, status}}),
});
test('reconnect updates in place and does not duplicate or reorder messages', () => {
  const tools = {kind:'tools'};
  const previous = [row('a', 10), tools, row('b', 12)];
  const updated = mergeMissionActivity(previous, [row('a', 10, 'done'), row('c', 11), row('d', 13)]);
  assert.deepEqual(updated.map(r => r.deliveryId ?? r.kind), ['a','tools','c','b','d']);
  assert.equal(updated[0].content, 'done');
  assert.deepEqual(mergeMissionActivity(updated, [row('a',10,'done'), row('d',13)]), updated);
});
test('an unchanged activity revision preserves the existing item', () => {
  const existing = row('a', 10, 'same', 4);
  const result = mergeMissionActivity([existing], [row('a', 10, 'same', 4)]);
  assert.equal(result[0], existing);
});
test('scheduler status changes replace a snapshot without a writer revision', () => {
  const existing = row('a', 10, 'same', 4, 'finishing');
  const result = mergeMissionActivity([existing], [row('a', 10, 'same', 4, 'done')]);
  assert.notEqual(result[0], existing);
  assert.equal(result[0].missionActivity.status, 'done');
});
test('old history cannot reappear at bottom of current page', () => {
  assert.deepEqual(mergeMissionActivity([row('b',20)], [row('a',1)]), [row('b',20)]);
});
test('empty specialist room receives its first assignment', () => {
  assert.deepEqual(mergeMissionActivity([], [row('a',1)]), [row('a',1)]);
});
