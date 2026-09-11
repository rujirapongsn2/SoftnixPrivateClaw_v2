import assert from "node:assert/strict";
import test from "node:test";
import { isSkillEnabledForCurrentUser } from "../.test-dist/skill-access.js";

test("keeps enabled built-in and owned skills available", () => {
  assert.equal(isSkillEnabledForCurrentUser({ enabled: true }), true);
  assert.equal(
    isSkillEnabledForCurrentUser({ enabled: true, read_only: true, subscription_enabled: true }),
    true,
  );
});

test("hides a shared skill until the recipient opts in", () => {
  assert.equal(
    isSkillEnabledForCurrentUser({ enabled: true, read_only: true, subscription_enabled: false }),
    false,
  );
  assert.equal(isSkillEnabledForCurrentUser({ enabled: true, read_only: true }), false);
});

test("hides a skill disabled by its owner", () => {
  assert.equal(
    isSkillEnabledForCurrentUser({ enabled: false, read_only: true, subscription_enabled: true }),
    false,
  );
  assert.equal(isSkillEnabledForCurrentUser({ enabled: false }), false);
});
