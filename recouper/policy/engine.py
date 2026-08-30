"""The gate every proposed action passes through.

One entry point, `PolicyEngine.gate()`. Nothing in the executor may contact a
customer or move money without a `GateVerdict` that allows it.

Two properties worth stating explicitly, because they are what make the audit
trail meaningful:

* **All rules are evaluated, always.** We do not short-circuit on the first
  denial. A verdict therefore records the complete picture -- "this was
  blocked by quiet_hours, and would also have been blocked by contact_cap"
  -- which is what lets an operator fix the real problem rather than the
  first one.

* **Denials are first-class output.** They are returned, logged, counted and
  displayed with the same weight as approvals. A recovery system whose
  refusals are invisible cannot be shown to be bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .rules import (
    DEFAULT_RULES,
    Decision,
    ProposedAction,
    Rule,
    RuleResult,
)


@dataclass(frozen=True)
class GateVerdict:
    decision: Decision
    reason: str
    checks: tuple[RuleResult, ...]

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def blocking_rules(self) -> tuple[str, ...]:
        return tuple(c.rule_id for c in self.checks if c.blocks)

    @property
    def is_retryable_later(self) -> bool:
        """Whether this denial is about timing rather than permission.

        Quiet hours and cooldowns are mistimings -- the action should be
        requeued. Everything else is a genuine prohibition and requeuing it
        would just produce the same denial forever.
        """
        timing_rules = {"quiet_hours", "contact_cooldown"}
        blocking = set(self.blocking_rules)
        return bool(blocking) and blocking.issubset(timing_rules)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "retryable_later": self.is_retryable_later,
            "checks": [c.to_dict() for c in self.checks],
        }


class PolicyEngine:
    def __init__(self, rules: Optional[Sequence[Rule]] = None) -> None:
        self.rules: tuple[Rule, ...] = tuple(
            rules if rules is not None else DEFAULT_RULES
        )
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate rule ids: {ids}")

    def gate(self, action: ProposedAction) -> GateVerdict:
        checks = tuple(rule.evaluate(action) for rule in self.rules)

        # A DENY anywhere outranks an ESCALATE: refusing to act is always
        # safe, whereas escalating a forbidden action puts it in front of a
        # human as though it were a live option.
        denials = [c for c in checks if c.decision is Decision.DENY]
        if denials:
            return GateVerdict(
                decision=Decision.DENY,
                reason=denials[0].reason,
                checks=checks,
            )

        escalations = [c for c in checks if c.decision is Decision.ESCALATE]
        if escalations:
            return GateVerdict(
                decision=Decision.ESCALATE,
                reason=escalations[0].reason,
                checks=checks,
            )

        return GateVerdict(
            decision=Decision.ALLOW,
            reason=f"all {len(checks)} policy checks passed",
            checks=checks,
        )

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.rules)
