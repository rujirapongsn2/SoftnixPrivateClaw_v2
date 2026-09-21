import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isArtifactListExpanded,
  toggleArtifactList,
} from '../.test-dist/sbot/artifact-list-state.js';

test('an expanded artifact list is collapsed synchronously for a different task', () => {
  const expandedA = toggleArtifactList({ key: '', expanded: false }, 'session-a');

  assert.equal(isArtifactListExpanded(expandedA, 'session-a'), true);
  assert.equal(isArtifactListExpanded(expandedA, 'session-b'), false);
});

test('the current artifact list can be expanded and collapsed', () => {
  const expanded = toggleArtifactList({ key: '', expanded: false }, 'session-a');
  const collapsed = toggleArtifactList(expanded, 'session-a');

  assert.equal(isArtifactListExpanded(collapsed, 'session-a'), false);
});
