"""Recovery planning.

The planner decides HOW to approach a case: which actions, in what order.
It does not decide WHETHER an action is permitted -- every plan it produces
is still gated action-by-action by the policy engine before anything runs.

Two implementations behind one interface:

* `DeterministicPlanner` -- a lookup from recovery class to action sequence.
  Fast, free, fully auditable, and the fallback whenever the LLM is
  unavailable or misbehaves.

* `LLMPlanner` -- asks Claude to choose among the actions the deterministic
  layer has already established are legal for this class, with a written
  rationale. Output is constrained to the ActionType enum and validated
  before use; anything unparseable falls back.

Why an LLM here at all, given the deterministic version works? Because plan
*selection* is where judgment genuinely helps -- weighing debt age against
amount against customer history to decide whether to lead with a retry or a
link, and whether a third contact is worth it. That is a matter of degree,
which is what models are good at. Classification and policy are matters of
correctness, which is what tables are good at.

The fallback path is not a degraded afterthought. It runs whenever the API
key is absent, so the whole system works with no LLM at all -- an important
property for a component that touches money.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..detect.classify import RecoveryClass
from ..detect.score import Case
from ..policy.rules import ActionType


@dataclass
class Plan:
    case_id: str
    actions: list[ActionType]
    rationale: str
    source: str  # "deterministic" | "llm" | "llm_fallback"

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "actions": [a.value for a in self.actions],
            "rationale": self.rationale,
            "source": self.source,
        }


# Which actions are even conceivable for each class. The planner may choose
# a subset and an ordering; it may not add anything not listed here.
#
# This is the enum the LLM is constrained to. A model cannot propose
# "offer_discount" or "call_customer" because those are not options it is
# given -- the safety property comes from the closed set, not from asking
# nicely in a prompt.
LEGAL_ACTIONS: dict[str, list[ActionType]] = {
    RecoveryClass.TRANSIENT.value: [
        ActionType.RETRY_PAYMENT,
        ActionType.CREATE_PAYMENT_LINK,
    ],
    RecoveryClass.DEFERRED.value: [
        ActionType.RETRY_PAYMENT,
        ActionType.SEND_REMINDER,
        ActionType.CREATE_PAYMENT_LINK,
    ],
    RecoveryClass.REAUTH.value: [
        ActionType.CREATE_PAYMENT_LINK,
        ActionType.SEND_REMINDER,
    ],
    RecoveryClass.INSTRUMENT_DEAD.value: [
        ActionType.CREATE_PAYMENT_LINK,
        ActionType.SEND_REMINDER,
    ],
    RecoveryClass.INTENT_NEGATIVE.value: [
        ActionType.SEND_REMINDER,  # one, and only one
    ],
    RecoveryClass.TERMINAL.value: [
        ActionType.CLOSE_NO_ACTION,
    ],
    RecoveryClass.UNCLASSIFIED.value: [
        ActionType.ESCALATE_TO_HUMAN,
    ],
    "abandoned_cart": [
        ActionType.CREATE_PAYMENT_LINK,
        ActionType.SEND_REMINDER,
    ],
    "overdue_invoice": [
        ActionType.SEND_REMINDER,
        ActionType.CREATE_PAYMENT_LINK,
        ActionType.ESCALATE_TO_HUMAN,
    ],
}


# The default sequence per class, used by the deterministic planner and as
# the LLM's fallback.
DEFAULT_PLANS: dict[str, list[ActionType]] = {
    RecoveryClass.TRANSIENT.value: [ActionType.RETRY_PAYMENT],
    RecoveryClass.DEFERRED.value: [
        ActionType.RETRY_PAYMENT,
        ActionType.SEND_REMINDER,
    ],
    RecoveryClass.REAUTH.value: [ActionType.CREATE_PAYMENT_LINK],
    RecoveryClass.INSTRUMENT_DEAD.value: [ActionType.CREATE_PAYMENT_LINK],
    RecoveryClass.INTENT_NEGATIVE.value: [ActionType.SEND_REMINDER],
    RecoveryClass.TERMINAL.value: [ActionType.CLOSE_NO_ACTION],
    RecoveryClass.UNCLASSIFIED.value: [ActionType.ESCALATE_TO_HUMAN],
    "abandoned_cart": [ActionType.CREATE_PAYMENT_LINK],
    "overdue_invoice": [ActionType.SEND_REMINDER],
}


class Planner(Protocol):
    def plan(self, case: Case) -> Plan: ...


@dataclass
class DeterministicPlanner:
    """Table-driven planning. Always available, never wrong in a new way."""

    def plan(self, case: Case) -> Plan:
        actions = list(DEFAULT_PLANS.get(case.case_class, [ActionType.ESCALATE_TO_HUMAN]))

        # A retry is only meaningful if the classification says we hold
        # something to charge. Dropping it here saves a guaranteed policy
        # denial and a wasted audit entry.
        if case.classification and not case.classification.can_retry_silently:
            actions = [a for a in actions if a is not ActionType.RETRY_PAYMENT]
        if not actions:
            actions = [ActionType.CREATE_PAYMENT_LINK]

        return Plan(
            case_id=case.case_id,
            actions=actions,
            rationale=(
                f"Default sequence for class '{case.case_class}'. "
                + (case.classification.rationale if case.classification else "")
            ),
            source="deterministic",
        )


SYSTEM_PROMPT = """You are a payment recovery planner for an Indian payments \
company. You choose how to approach one at-risk debt.

You are NOT the safety layer. A separate deterministic policy engine will gate \
every action you propose against contact caps, quiet hours, do-not-contact \
lists, amount ceilings and dispute flags. Do not try to reason about those \
constraints -- propose what is most likely to recover the money, and let the \
gate do its job.

Rules you MUST follow:
- Choose ONLY from the allowed actions given to you. Never invent an action.
- Order matters: list actions in the sequence they should be attempted.
- Prefer fewer actions. Every customer contact has a real cost in goodwill, \
and recovery value decays sharply after the first contact.
- Respond with ONLY a JSON object, no prose around it:
  {"actions": ["action_name", ...], "rationale": "one or two sentences"}
"""


@dataclass
class LLMPlanner:
    """Claude-backed planner with a deterministic fallback."""

    model: str = "claude-sonnet-5"
    api_key: Optional[str] = None
    fallback: DeterministicPlanner = field(default_factory=DeterministicPlanner)
    max_tokens: int = 400
    _client: object = field(default=None, init=False, repr=False)

    stats: dict = field(
        default_factory=lambda: {"llm_ok": 0, "fallback": 0, "invalid": 0}
    )

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return
        try:
            import anthropic

            self._client = anthropic.Anthropic(api_key=key)
        except Exception:
            # Missing SDK or bad credentials -- fall back silently rather
            # than taking the batch down. Recovery work is more valuable
            # than optimal plan selection.
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def plan(self, case: Case) -> Plan:
        if self._client is None:
            self.stats["fallback"] += 1
            p = self.fallback.plan(case)
            p.source = "llm_fallback"
            p.rationale = "LLM unavailable; " + p.rationale
            return p

        allowed = LEGAL_ACTIONS.get(case.case_class, [ActionType.ESCALATE_TO_HUMAN])
        if case.classification and not case.classification.can_retry_silently:
            allowed = [a for a in allowed if a is not ActionType.RETRY_PAYMENT]

        try:
            raw = self._call(case, allowed)
            parsed = self._parse(raw, allowed)
        except Exception:
            self.stats["fallback"] += 1
            p = self.fallback.plan(case)
            p.source = "llm_fallback"
            p.rationale = "LLM call failed; " + p.rationale
            return p

        if parsed is None:
            self.stats["invalid"] += 1
            p = self.fallback.plan(case)
            p.source = "llm_fallback"
            p.rationale = "LLM output rejected by schema; " + p.rationale
            return p

        actions, rationale = parsed
        self.stats["llm_ok"] += 1
        return Plan(case.case_id, actions, rationale, "llm")

    def _call(self, case: Case, allowed: list[ActionType]) -> str:
        user = json.dumps(
            {
                "case_kind": case.kind,
                "recovery_class": case.case_class,
                "amount_rupees": case.amount_paise / 100,
                "age_days": round(case.age_s / 86_400, 1),
                "classification_rationale": (
                    case.classification.rationale if case.classification else None
                ),
                "allowed_actions": [a.value for a in allowed],
            },
            indent=2,
        )
        resp = self._client.messages.create(  # type: ignore[union-attr]
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text

    @staticmethod
    def _parse(
        raw: str, allowed: list[ActionType]
    ) -> Optional[tuple[list[ActionType], str]]:
        """Validate model output against the allowed set.

        Rejects rather than repairs. A plan we had to guess the meaning of is
        not a plan we should execute against someone's money.
        """
        text = raw.strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

        names = data.get("actions")
        if not isinstance(names, list) or not names:
            return None

        allowed_values = {a.value for a in allowed}
        actions: list[ActionType] = []
        for name in names:
            if not isinstance(name, str) or name not in allowed_values:
                # Any action outside the allowed set invalidates the whole
                # plan. Silently dropping it would execute a plan the model
                # did not actually propose.
                return None
            actions.append(ActionType(name))

        rationale = data.get("rationale", "")
        if not isinstance(rationale, str):
            rationale = ""
        return actions, rationale[:500]
