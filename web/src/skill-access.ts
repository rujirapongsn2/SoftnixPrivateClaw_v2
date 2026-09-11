/**
 * Returns whether a skill belongs in the current user's active skill list.
 * Shared skills retain their owner's `enabled` state, so recipients must also
 * opt in through `subscription_enabled` before they can select one.
 */
export function isSkillEnabledForCurrentUser(skill: {
  enabled: boolean;
  read_only?: boolean;
  subscription_enabled?: boolean;
}): boolean {
  return skill.enabled && (!skill.read_only || skill.subscription_enabled === true);
}
