"""How far a skill may be shared, from its security review and the admin's policy.

Pure: the store passes the stored review, decision and policy in. The policy applies when a skill is
read, never by rewriting the owner's visibility, so relaxing it later brings existing shares back.

A scope is the widest audience a skill may reach: `public` > `group` > `owner` > `blocked`.
"""

from __future__ import annotations

from dataclasses import dataclass

POLICY_KEY = "skill_security_policy"
LEVELS = ("low", "medium", "high", "critical")
SCOPES = ("public", "group", "owner", "blocked")
DEFAULT_POLICY = {"critical": "owner", "high": "group", "medium": "public", "low": "public"}
_RANK = {scope: i for i, scope in enumerate(SCOPES)}
_VISIBILITY_NEEDS = {"public": "public", "group": "group", "private": "owner"}


@dataclass(frozen=True)
class Review:
    sha256: str
    max_severity: str  # none|low|medium|high|critical
    ast_codes: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    decision: str  # requested|allow|reject|block
    sha256: str
    scope: str | None


@dataclass(frozen=True)
class Verdict:
    scope: str
    reason: str  # pending|policy|exception|blocked
    max_severity: str | None
    ast_codes: tuple[str, ...]


def parse_policy(value: dict | None) -> dict[str, str]:
    """The stored policy with defaults for missing levels. Raises ValueError for an invalid one."""
    policy = {**DEFAULT_POLICY, **{k: v for k, v in (value or {}).items() if k in LEVELS}}
    if any(v not in SCOPES for v in policy.values()):
        raise ValueError("Each level must be one of: " + ", ".join(SCOPES))
    for lower, higher in zip(LEVELS, LEVELS[1:]):
        if _RANK[policy[higher]] < _RANK[policy[lower]]:
            raise ValueError(f"{higher.title()} cannot be shared more widely than {lower.title()}")
    return policy


def verdict(policy: dict[str, str], review: Review | None, decision: Decision | None) -> Verdict:
    severity = review.max_severity if review else None
    codes = review.ast_codes if review else ()
    if decision is not None and decision.decision == "block":
        return Verdict("blocked", "blocked", severity, codes)
    if review is None:  # not reviewed yet, or the last review failed: the owner keeps using it
        return Verdict("owner", "pending", None, ())
    by_policy = policy.get(severity, "public")
    if decision is not None and decision.decision == "allow" and decision.sha256 == review.sha256 and decision.scope in SCOPES:
        # An exception only ever widens: approving never cuts off a share the policy already allows.
        if _RANK[decision.scope] < _RANK[by_policy]:
            return Verdict(decision.scope, "exception", severity, codes)
    return Verdict(by_policy, "policy", severity, codes)


def permits(scope: str, visibility: str) -> bool:
    """Whether a skill limited to `scope` may be shared at `visibility` (private, group, public)."""
    return _RANK[scope] <= _RANK[_VISIBILITY_NEEDS.get(visibility, "owner")]


def readable(scope: str, visibility: str, is_owner: bool) -> bool:
    """Whether a reader may use the skill: its owner unless blocked, anyone else within the scope."""
    if scope == "blocked":
        return False
    return is_owner or (visibility in ("group", "public") and permits(scope, visibility))


def widens(current: str, wanted: str) -> bool:
    """Whether moving a skill from visibility `current` to `wanted` reaches more people."""
    return _RANK[_VISIBILITY_NEEDS.get(wanted, "owner")] < _RANK[_VISIBILITY_NEEDS.get(current, "owner")]


def widest_visibility(scope: str) -> str | None:
    return {"public": "public", "group": "group", "owner": "private"}.get(scope)
