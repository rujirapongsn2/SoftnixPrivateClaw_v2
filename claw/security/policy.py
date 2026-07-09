"""Control policy engine — the agent's safety layer.

Text crossing a boundary (user input, model output, tool arguments) is checked
against ordered rules. A rule can:
- mask   : replace matches with a placeholder (e.g. [REDACTED_EMAIL])
- block  : refuse the whole action, returning a safe message
- monitor: record a hit for audit but let the text pass unchanged

Rules are pure regex + action, so decisions are deterministic and cheap; the
engine never calls an LLM. Built-in PII/secret patterns ship enabled; operators
can add custom rules per instance.
"""

import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Action(str, Enum):
    MASK = "mask"
    BLOCK = "block"
    MONITOR = "monitor"


Scope = str  # "input" | "output" | "tool_args"


@dataclass(frozen=True, slots=True)
class PolicyRule:
    name: str
    pattern: str
    action: Action
    scopes: tuple[Scope, ...] = ("input", "output", "tool_args")
    placeholder: str = "[REDACTED]"
    severity: str = "medium"
    enabled: bool = True
    # Human message returned when this rule blocks.
    block_message: str = "This request was blocked by the control policy."

    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern, re.IGNORECASE)


@dataclass(slots=True)
class PolicyDecision:
    action: Action | None
    text: str  # possibly-masked text (equals input when nothing matched)
    matched_rules: list[str] = field(default_factory=list)
    severity: str = "info"
    message: str | None = None  # set when blocked

    @property
    def blocked(self) -> bool:
        return self.action is Action.BLOCK

    @property
    def masked(self) -> bool:
        return self.action is Action.MASK


# Built-in PII / secret rules. Ordered: block rules should precede mask rules.
_BUILTINS: tuple[PolicyRule, ...] = (
    PolicyRule(
        name="credit_card",
        pattern=r"\b(?:\d[ -]*?){13,16}\b",
        action=Action.MASK,
        placeholder="[REDACTED_CARD]",
        severity="high",
    ),
    PolicyRule(
        name="email",
        pattern=r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        action=Action.MASK,
        placeholder="[REDACTED_EMAIL]",
    ),
    PolicyRule(
        name="api_key",
        pattern=r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}\b|\bAKIA[0-9A-Z]{16}\b",
        action=Action.MASK,
        placeholder="[REDACTED_SECRET]",
        severity="high",
    ),
    PolicyRule(
        name="thai_national_id",
        pattern=r"\b\d-?\d{4}-?\d{5}-?\d{2}-?\d\b",
        action=Action.MASK,
        placeholder="[REDACTED_ID]",
        severity="high",
    ),
)


# Tools exempt from tool_args enforcement by default: the bundled communication
# connectors whose payload legitimately IS PII (a recipient email, an attendee).
# Masking their arguments would break the action (e.g. an Outlook draft created
# with a [REDACTED_EMAIL] recipient). MCP tools are named mcp_{connector}_{tool},
# so these globs target whole connectors. Admins can edit the list at runtime.
DEFAULT_TOOL_ARGS_EXEMPT: tuple[str, ...] = (
    "mcp_outlook_*",
    "mcp_outlook-calendar_*",
    "mcp_gmail_*",
)


class PolicyEngine:
    def __init__(
        self,
        rules: Iterable[PolicyRule] | None = None,
        *,
        monitor_only: bool = False,
        tool_args_exempt: Iterable[str] | None = None,
    ):
        self.rules: list[PolicyRule] = list(rules if rules is not None else _BUILTINS)
        # Global kill-switch: when True, block/mask are downgraded to monitor
        # so operators can observe hits before enforcing.
        self.monitor_only = monitor_only
        # Tool-name globs exempt from tool_args masking/blocking (input/output
        # scopes are never exempt). Defaults cover the communication connectors.
        self.tool_args_exempt: list[str] = list(
            tool_args_exempt if tool_args_exempt is not None else DEFAULT_TOOL_ARGS_EXEMPT
        )

    def reload(
        self,
        rules: Iterable[PolicyRule],
        monitor_only: bool | None = None,
        tool_args_exempt: Iterable[str] | None = None,
    ) -> None:
        """Swap the active ruleset (and optionally the monitor toggle / tool-args
        exemption list) atomically.

        Called at startup and after any admin guardrail edit so enforcement in
        `enforce()` reflects the persisted configuration without a restart.
        """
        self.rules = list(rules)
        if monitor_only is not None:
            self.monitor_only = monitor_only
        if tool_args_exempt is not None:
            self.tool_args_exempt = list(tool_args_exempt)

    def is_tool_exempt(self, tool_name: str) -> bool:
        """True when `tool_name` matches any exemption glob — its arguments skip
        tool_args masking/blocking (they are still detected + audited upstream)."""
        if not tool_name:
            return False
        name = tool_name.lower()
        return any(fnmatch.fnmatch(name, glob.lower()) for glob in self.tool_args_exempt)

    def enforce(self, text: str, scope: Scope) -> PolicyDecision:
        if not text:
            return PolicyDecision(action=None, text=text)

        working = text
        matched: list[str] = []
        highest_severity = "info"
        effective_action: Action | None = None

        for rule in self.rules:
            if not rule.enabled or scope not in rule.scopes:
                continue
            regex = rule.compiled()
            if not regex.search(working):
                continue
            matched.append(rule.name)
            highest_severity = _max_severity(highest_severity, rule.severity)

            action = Action.MONITOR if self.monitor_only else rule.action
            if action is Action.BLOCK:
                return PolicyDecision(
                    action=Action.BLOCK,
                    text=working,
                    matched_rules=matched,
                    severity=highest_severity,
                    message=rule.block_message,
                )
            if action is Action.MASK:
                working = regex.sub(rule.placeholder, working)
                effective_action = Action.MASK
            elif effective_action is None:
                effective_action = Action.MONITOR

        return PolicyDecision(
            action=effective_action,
            text=working,
            matched_rules=matched,
            severity=highest_severity if matched else "info",
        )


_SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _max_severity(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER.get(a, 0) >= _SEVERITY_ORDER.get(b, 0) else b


# -- persistence bridge (DB <-> engine) -------------------------------------

def builtin_rule_seeds() -> list[dict]:
    """Built-in rules as plain dicts, for one-time seeding into GuardrailStore."""
    return [
        {
            "name": r.name,
            "pattern": r.pattern,
            "action": r.action.value,
            "scopes": list(r.scopes),
            "placeholder": r.placeholder,
            "severity": r.severity,
            "block_message": r.block_message,
            "enabled": r.enabled,
            "is_builtin": True,
        }
        for r in _BUILTINS
    ]


def rule_from_row(row) -> PolicyRule:
    """Build a PolicyRule from a GuardrailRule ORM row (or any object with the fields)."""
    action = row.action if isinstance(row.action, Action) else Action(str(row.action))
    scopes = tuple(row.scopes or ("input", "output", "tool_args"))
    return PolicyRule(
        name=row.name,
        pattern=row.pattern,
        action=action,
        scopes=scopes,
        placeholder=row.placeholder or "[REDACTED]",
        severity=row.severity or "medium",
        enabled=bool(row.enabled),
        block_message=row.block_message or "This request was blocked by the control policy.",
    )
