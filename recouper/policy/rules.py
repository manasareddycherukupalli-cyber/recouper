"""Policy rules: the hard bounds on what the agent may do.

Design commitments, in order of importance:

1. **Deterministic.** No rule consults an LLM. A bound that a model can be
   argued out of is not a bound -- it makes prompt phrasing a security
   boundary. The planner may propose anything it likes; these rules decide.

2. **Named and individually testable.** Each rule is its own class with an
   id, so a denial can be reported as "denied by contact_frequency_cap"
   rather than an opaque False. The audit trail records every rule that was
   evaluated, not just the one that failed.

3. **Deny by default on missing information.** If a rule cannot establish
   that an action is safe -- unknown customer, unreadable history -- it
   denies. The cost of a wrongly-skipped recovery is one unrecovered debt;
   the cost of a wrongly-sent contact is a harmed customer relationship and
   potentially a compliance breach. These are not symmetric.

4. **No override path.** There is deliberately no `force=True`. A rule that
   can be bypassed under pressure is documentation, not policy.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

_DAY = 86_400
IST = timezone(timedelta(hours=5, minutes=30))


class ActionType(enum.Enum):
    """The complete set of actions the agent may ever propose.

    Closed on purpose. The planner's structured output is constrained to this
    enum, so the model cannot invent an action the policy layer has never
    heard of and therefore has no rule for.
    """

    RETRY_PAYMENT = "retry_payment"
    CREATE_PAYMENT_LINK = "create_payment_link"
    SEND_REMINDER = "send_reminder"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    CLOSE_NO_ACTION = "close_no_action"

    @property
    def contacts_customer(self) -> bool:
        return self in (ActionType.CREATE_PAYMENT_LINK, ActionType.SEND_REMINDER)

    @property
    def moves_money(self) -> bool:
        return self is ActionType.RETRY_PAYMENT


class Decision(enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    decision: Decision
    reason: str

    @property
    def blocks(self) -> bool:
        return self.decision is not Decision.ALLOW

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "decision": self.decision.value,
            "reason": self.reason,
        }


@dataclass
class CustomerState:
    """What we know about our prior treatment of this customer.

    Sourced from the audit ledger, not from the provider -- these are facts
    about *our* behaviour, and the ledger is their system of record.
    """

    customer_id: str
    do_not_contact: bool = False
    contacts_by_debt: dict[str, int] = field(default_factory=dict)
    last_contact_ts: Optional[int] = None
    known: bool = True

    def contacts_for(self, debt_id: str) -> int:
        return self.contacts_by_debt.get(debt_id, 0)


@dataclass
class DebtState:
    """The debt being worked, and any events that should stop us."""

    debt_id: str
    customer_id: str
    amount: int  # paise
    retries_attempted: int = 0
    max_retries_allowed: int = 0
    class_allows_contact: bool = True
    class_allows_silent_retry: bool = False
    # Hard-stop signals.
    has_open_dispute: bool = False
    has_chargeback: bool = False
    was_refunded: bool = False
    customer_replied: bool = False
    is_settled: bool = False


@dataclass
class ProposedAction:
    action: ActionType
    debt: DebtState
    customer: CustomerState
    now_ts: int
    rationale: str = ""


class Rule(Protocol):
    id: str

    def evaluate(self, action: ProposedAction) -> RuleResult: ...


def _allow(rule_id: str, reason: str = "ok") -> RuleResult:
    return RuleResult(rule_id, Decision.ALLOW, reason)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class DoNotContactRule:
    """Absolute. Checked first, no exceptions, no override.

    A do-not-contact flag is frequently a legal obligation rather than a
    preference. It is the one rule where even a high-value debt does not buy
    a different answer -- so it deliberately does not consult the amount.
    """

    id = "do_not_contact"

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if not a.action.contacts_customer:
            return _allow(self.id, "action does not contact the customer")
        if a.customer.do_not_contact:
            return RuleResult(
                self.id,
                Decision.DENY,
                "customer is on the do-not-contact list; no contact is "
                "permissible regardless of debt value",
            )
        return _allow(self.id)


class UnknownCustomerRule:
    """Deny-by-default when we cannot establish who we are contacting."""

    id = "unknown_customer"

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if not a.action.contacts_customer:
            return _allow(self.id, "action does not contact the customer")
        if not a.customer.known:
            return RuleResult(
                self.id,
                Decision.DENY,
                "no contact history could be established for this customer; "
                "denying rather than assuming a clean slate",
            )
        return _allow(self.id)


class HardStopRule:
    """Events after which continuing to chase is wrong.

    A dispute or chargeback means the debt is contested -- pursuing it is
    both legally fraught and likely to escalate. A refund or settlement means
    there is no debt. A customer reply means a human is engaged and an
    automated sequence talking over them is the worst outcome.
    """

    id = "hard_stop"

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if a.action in (ActionType.CLOSE_NO_ACTION, ActionType.ESCALATE_TO_HUMAN):
            return _allow(self.id, "terminal actions are always permitted")

        d = a.debt
        for flag, why in (
            (d.has_open_dispute, "an open dispute exists on this debt"),
            (d.has_chargeback, "a chargeback has been raised"),
            (d.was_refunded, "this payment was refunded"),
            (d.is_settled, "the debt is already settled"),
            (d.customer_replied, "the customer has replied; a human owns this now"),
        ):
            if flag:
                return RuleResult(self.id, Decision.DENY, f"halted because {why}")
        return _allow(self.id)


class ContactCapRule:
    """At most 3 contacts per debt, lifetime.

    Recovery value decays sharply with contact count while annoyance grows
    linearly. Three is the point past which additional contacts mostly buy
    unsubscribes rather than payments.
    """

    id = "contact_cap_per_debt"
    MAX_CONTACTS = 3

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if not a.action.contacts_customer:
            return _allow(self.id, "action does not contact the customer")
        sent = a.customer.contacts_for(a.debt.debt_id)
        if sent >= self.MAX_CONTACTS:
            return RuleResult(
                self.id,
                Decision.DENY,
                f"{sent} contacts already sent for this debt "
                f"(cap {self.MAX_CONTACTS})",
            )
        return _allow(self.id, f"{sent}/{self.MAX_CONTACTS} contacts used")


class ContactCooldownRule:
    """At most one contact per customer per 72h, across ALL debts.

    The per-debt cap alone is not enough: a customer with four overdue
    invoices could legally receive twelve messages. The customer experiences
    us as one sender, so the cooldown is scoped to the person, not the debt.
    """

    id = "contact_cooldown"
    COOLDOWN_S = 3 * _DAY

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if not a.action.contacts_customer:
            return _allow(self.id, "action does not contact the customer")
        last = a.customer.last_contact_ts
        if last is None:
            return _allow(self.id, "no prior contact")
        elapsed = a.now_ts - last
        if elapsed < self.COOLDOWN_S:
            hrs = elapsed // 3600
            return RuleResult(
                self.id,
                Decision.DENY,
                f"last contact was {hrs}h ago; cooldown is "
                f"{self.COOLDOWN_S // 3600}h across all debts",
            )
        return _allow(self.id)


class QuietHoursRule:
    """No contact 21:00-09:00 IST.

    Denies with a distinct reason so the executor requeues rather than
    abandoning: the action is not wrong, only mistimed. Dropping it would
    silently lose recoverable debt every night.
    """

    id = "quiet_hours"
    START_HOUR = 21
    END_HOUR = 9

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if not a.action.contacts_customer:
            return _allow(self.id, "action does not contact the customer")
        hour = datetime.fromtimestamp(a.now_ts, tz=IST).hour
        if hour >= self.START_HOUR or hour < self.END_HOUR:
            return RuleResult(
                self.id,
                Decision.DENY,
                f"local time {hour:02d}:xx IST falls in quiet hours "
                f"({self.START_HOUR}:00-{self.END_HOUR}:00); requeue for "
                f"{self.END_HOUR}:00",
            )
        return _allow(self.id)


class RetryEligibilityRule:
    """A retry needs both a permitting failure class and a live budget.

    Mirrors the two-condition gate in detect/classify.py. Enforced again here
    rather than trusted, because this is the layer that actually moves money
    and it should not depend on an upstream module having been correct.
    """

    id = "retry_eligibility"

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if not a.action.moves_money:
            return _allow(self.id, "action does not move money")
        d = a.debt
        if not d.class_allows_silent_retry:
            return RuleResult(
                self.id,
                Decision.DENY,
                "failure class does not permit a silent retry (no reusable "
                "instrument, or the class can never succeed on retry)",
            )
        if d.retries_attempted >= d.max_retries_allowed:
            return RuleResult(
                self.id,
                Decision.DENY,
                f"retry budget exhausted: {d.retries_attempted}/"
                f"{d.max_retries_allowed}",
            )
        return _allow(
            self.id, f"{d.retries_attempted}/{d.max_retries_allowed} retries used"
        )


class ClassContactRule:
    """Some failure classes forbid contact outright (fraud, unclassified)."""

    id = "class_allows_contact"

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if not a.action.contacts_customer:
            return _allow(self.id, "action does not contact the customer")
        if not a.debt.class_allows_contact:
            return RuleResult(
                self.id,
                Decision.DENY,
                "failure class forbids customer contact (fraud-flagged, or "
                "an unrecognised failure reason we will not act on)",
            )
        return _allow(self.id)


class AmountCeilingRule:
    """Debts above Rs.50,000 require a human.

    Not because the automation is likelier to be wrong on large debts, but
    because the cost of being wrong scales with the amount while our
    confidence does not. High-value customers are also exactly the ones a
    clumsy dunning message does most damage to.

    Escalates rather than denies -- the debt still gets worked, by a person.
    """

    id = "amount_ceiling"
    CEILING_PAISE = 50_000 * 100

    def evaluate(self, a: ProposedAction) -> RuleResult:
        if a.action in (ActionType.ESCALATE_TO_HUMAN, ActionType.CLOSE_NO_ACTION):
            return _allow(self.id, "terminal actions are always permitted")
        if a.debt.amount > self.CEILING_PAISE:
            rupees = a.debt.amount / 100
            return RuleResult(
                self.id,
                Decision.ESCALATE,
                f"debt of Rs.{rupees:,.0f} exceeds the Rs."
                f"{self.CEILING_PAISE // 100:,} automation ceiling; routing "
                f"to a human",
            )
        return _allow(self.id)


# Order matters for reporting: the first blocking rule is the headline reason,
# so the most fundamental prohibitions are evaluated first.
DEFAULT_RULES: list[Rule] = [
    DoNotContactRule(),
    UnknownCustomerRule(),
    HardStopRule(),
    ClassContactRule(),
    AmountCeilingRule(),
    ContactCapRule(),
    ContactCooldownRule(),
    QuietHoursRule(),
    RetryEligibilityRule(),
]
