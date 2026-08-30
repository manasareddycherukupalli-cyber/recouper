"""Adversarial tests for the policy engine.

Framing: every test here is the planner trying to do something it must not be
allowed to do. The LLM in this system is untrusted by construction -- not
because it is malicious, but because a component whose output depends on
prompt phrasing cannot be a safety boundary. These tests are the boundary.

If any test in this file fails, the claim "every money action is bounded and
gated" is false.
"""

import pytest

from recouper.policy.engine import PolicyEngine
from recouper.policy.rules import (
    ActionType,
    CustomerState,
    DebtState,
    Decision,
    ProposedAction,
)

# 2026-08-30 14:00 IST -- inside business hours, so quiet-hours does not
# confound tests that are about something else.
NOON_IST = 1_788_078_600
# 2026-08-30 03:00 IST.
THREE_AM_IST = 1_788_039_000

engine = PolicyEngine()


def customer(**kw) -> CustomerState:
    base = dict(customer_id="cust_A", do_not_contact=False, known=True)
    base.update(kw)
    return CustomerState(**base)  # type: ignore[arg-type]


def debt(**kw) -> DebtState:
    base = dict(
        debt_id="pay_A",
        customer_id="cust_A",
        amount=250_000,  # Rs.2,500
        retries_attempted=0,
        max_retries_allowed=2,
        class_allows_contact=True,
        class_allows_silent_retry=True,
    )
    base.update(kw)
    return DebtState(**base)  # type: ignore[arg-type]


def propose(action: ActionType, *, now=NOON_IST, cust=None, dbt=None) -> ProposedAction:
    return ProposedAction(
        action=action,
        debt=dbt or debt(),
        customer=cust or customer(),
        now_ts=now,
        rationale="proposed by planner",
    )


# --- the baseline: the system must actually work ---------------------------


def test_a_clean_reminder_is_allowed():
    """Sanity: if everything is fine, the agent is not blocked.

    A policy engine that denies everything is trivially 'safe' and useless.
    """
    v = engine.gate(propose(ActionType.SEND_REMINDER))
    assert v.allowed
    assert v.blocking_rules == ()


def test_a_clean_retry_is_allowed():
    v = engine.gate(propose(ActionType.RETRY_PAYMENT))
    assert v.allowed


# --- do not contact ---------------------------------------------------------


def test_dnc_customer_cannot_be_contacted():
    v = engine.gate(propose(ActionType.SEND_REMINDER, cust=customer(do_not_contact=True)))
    assert v.decision is Decision.DENY
    assert "do_not_contact" in v.blocking_rules


def test_dnc_holds_even_for_a_large_debt():
    """The most likely place for a bypass to be rationalised.

    A large debt is exactly when someone is tempted to make an exception.
    DNC is frequently a legal obligation, so the amount must not enter into
    it. Note this correctly escalates on amount too -- but it must never
    come back ALLOW.
    """
    v = engine.gate(
        propose(
            ActionType.SEND_REMINDER,
            cust=customer(do_not_contact=True),
            dbt=debt(amount=9_000_000),  # Rs.90,000
        )
    )
    assert v.decision is Decision.DENY
    assert not v.allowed


def test_dnc_does_not_block_a_silent_retry():
    """DNC governs contact, not charging a mandate the customer granted.

    Conflating the two would strand recoverable debt for no compliance gain.
    """
    v = engine.gate(propose(ActionType.RETRY_PAYMENT, cust=customer(do_not_contact=True)))
    assert v.allowed


# --- hard stops -------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    ["has_open_dispute", "has_chargeback", "was_refunded", "is_settled", "customer_replied"],
)
@pytest.mark.parametrize(
    "action", [ActionType.SEND_REMINDER, ActionType.RETRY_PAYMENT, ActionType.CREATE_PAYMENT_LINK]
)
def test_hard_stop_flags_block_every_pursuit_action(flag, action):
    """Disputed, refunded, settled or human-owned debts are not pursued."""
    v = engine.gate(propose(action, dbt=debt(**{flag: True})))
    assert v.decision is Decision.DENY
    assert "hard_stop" in v.blocking_rules


def test_escalation_survives_a_hard_stop():
    """Handing a stopped case to a human must always remain possible."""
    v = engine.gate(propose(ActionType.ESCALATE_TO_HUMAN, dbt=debt(has_open_dispute=True)))
    assert v.allowed


# --- fraud / unclassified ---------------------------------------------------


def test_fraud_class_forbids_contact():
    """Fraud-flagged debts: the person we would contact is likely a victim."""
    v = engine.gate(
        propose(ActionType.SEND_REMINDER, dbt=debt(class_allows_contact=False))
    )
    assert v.decision is Decision.DENY
    assert "class_allows_contact" in v.blocking_rules


# --- frequency caps ---------------------------------------------------------


def test_fourth_contact_on_a_debt_is_denied():
    v = engine.gate(
        propose(
            ActionType.SEND_REMINDER,
            cust=customer(contacts_by_debt={"pay_A": 3}, last_contact_ts=None),
        )
    )
    assert v.decision is Decision.DENY
    assert "contact_cap_per_debt" in v.blocking_rules


def test_cooldown_is_scoped_to_the_person_not_the_debt():
    """The multi-debt pile-on case.

    A customer with several overdue debts would otherwise legally receive a
    message per debt on the same day. They experience us as one sender, so
    the cooldown must span all debts. This test is the reason the rule is
    keyed on last_contact_ts rather than per-debt history.
    """
    v = engine.gate(
        propose(
            ActionType.SEND_REMINDER,
            dbt=debt(debt_id="pay_DIFFERENT"),  # a different debt entirely
            cust=customer(
                contacts_by_debt={"pay_A": 1},
                last_contact_ts=NOON_IST - 3600,  # contacted an hour ago
            ),
        )
    )
    assert v.decision is Decision.DENY
    assert "contact_cooldown" in v.blocking_rules


def test_contact_allowed_once_cooldown_has_elapsed():
    v = engine.gate(
        propose(
            ActionType.SEND_REMINDER,
            cust=customer(last_contact_ts=NOON_IST - 4 * 86_400),
        )
    )
    assert v.allowed


# --- quiet hours ------------------------------------------------------------


def test_no_contact_at_three_am():
    v = engine.gate(propose(ActionType.SEND_REMINDER, now=THREE_AM_IST))
    assert v.decision is Decision.DENY
    assert "quiet_hours" in v.blocking_rules


def test_quiet_hours_denial_is_requeueable_not_terminal():
    """A mistimed action must be requeued, not dropped.

    If quiet-hours denials were treated like any other refusal, we would
    silently lose every recoverable debt that came up overnight. The engine
    distinguishes timing denials precisely so the executor can requeue them.
    """
    v = engine.gate(propose(ActionType.SEND_REMINDER, now=THREE_AM_IST))
    assert v.is_retryable_later is True


def test_a_permission_denial_is_not_requeueable():
    """Contrast case: requeuing a DNC denial forever would be a bug."""
    v = engine.gate(propose(ActionType.SEND_REMINDER, cust=customer(do_not_contact=True)))
    assert v.is_retryable_later is False


def test_quiet_hours_does_not_block_a_silent_retry():
    """Nobody is woken by a background charge attempt."""
    v = engine.gate(propose(ActionType.RETRY_PAYMENT, now=THREE_AM_IST))
    assert v.allowed


# --- retry budget -----------------------------------------------------------


def test_retry_denied_when_class_forbids_silent_retry():
    v = engine.gate(
        propose(ActionType.RETRY_PAYMENT, dbt=debt(class_allows_silent_retry=False))
    )
    assert v.decision is Decision.DENY
    assert "retry_eligibility" in v.blocking_rules


def test_retry_denied_once_budget_exhausted():
    v = engine.gate(
        propose(
            ActionType.RETRY_PAYMENT,
            dbt=debt(retries_attempted=2, max_retries_allowed=2),
        )
    )
    assert v.decision is Decision.DENY
    assert "retry_eligibility" in v.blocking_rules


# --- amount ceiling ---------------------------------------------------------


def test_large_debt_escalates_rather_than_auto_acting():
    """Escalate, not deny -- the debt still gets worked, by a person."""
    v = engine.gate(propose(ActionType.SEND_REMINDER, dbt=debt(amount=6_000_000)))
    assert v.decision is Decision.ESCALATE
    assert not v.allowed


def test_deny_outranks_escalate():
    """A forbidden action must never reach a human as a live option.

    If escalation won, an operator would be shown a DNC-violating contact
    and asked to approve it. The safe composition is that any denial wins.
    """
    v = engine.gate(
        propose(
            ActionType.SEND_REMINDER,
            cust=customer(do_not_contact=True),
            dbt=debt(amount=6_000_000),
        )
    )
    assert v.decision is Decision.DENY


# --- deny by default --------------------------------------------------------


def test_unknown_customer_is_denied_not_assumed_clean():
    v = engine.gate(propose(ActionType.SEND_REMINDER, cust=customer(known=False)))
    assert v.decision is Decision.DENY
    assert "unknown_customer" in v.blocking_rules


# --- auditability -----------------------------------------------------------


def test_every_rule_is_evaluated_even_after_a_denial():
    """No short-circuiting.

    An operator needs the full picture. If we stopped at the first denial,
    fixing it would just reveal the next one, one deploy at a time.
    """
    v = engine.gate(
        propose(
            ActionType.SEND_REMINDER,
            now=THREE_AM_IST,
            cust=customer(do_not_contact=True, contacts_by_debt={"pay_A": 5}),
        )
    )
    assert len(v.checks) == len(engine.rules)
    assert len(v.blocking_rules) >= 3


def test_every_denial_carries_a_human_readable_reason():
    v = engine.gate(propose(ActionType.SEND_REMINDER, cust=customer(do_not_contact=True)))
    assert len(v.reason) > 20
    for check in v.checks:
        assert check.reason, f"{check.rule_id} produced an empty reason"


def test_no_duplicate_rule_ids():
    ids = engine.rule_ids()
    assert len(ids) == len(set(ids))


def test_fixture_timestamps_are_when_they_claim_to_be():
    """Guards the fixtures themselves.

    The first version of these constants was a year out (2025, not 2026).
    Every test still passed, because the *hour* happened to be right and the
    hour is all the quiet-hours rule reads. A fixture that is wrong in a way
    the suite cannot see is worse than no fixture, so assert the full
    datetime, not just the behaviour it produces.
    """
    from datetime import datetime

    from recouper.data.generate import DEFAULT_NOW
    from recouper.policy.rules import IST

    noon = datetime.fromtimestamp(NOON_IST, tz=IST)
    assert (noon.year, noon.month, noon.day, noon.hour) == (2026, 8, 30, 14)

    early = datetime.fromtimestamp(THREE_AM_IST, tz=IST)
    assert (early.year, early.month, early.day, early.hour) == (2026, 8, 30, 3)

    now = datetime.fromtimestamp(DEFAULT_NOW, tz=IST)
    assert now.year == 2026, "corpus 'now' must be the current year"


def test_there_is_no_override_parameter():
    """A bound that can be bypassed under pressure is documentation.

    Guards against a future 'just add force=True for the urgent case' patch.
    """
    import inspect

    sig = inspect.signature(engine.gate)
    assert list(sig.parameters) == ["action"]
