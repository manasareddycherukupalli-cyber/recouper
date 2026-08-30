"""Tests for the retryability taxonomy.

The important cases here are the refusals. It is easy to write a recovery
system that recovers; the risk is one that retries a stolen card, or hammers
an expired instrument that can never succeed. Those tests come first.
"""

import pytest

from recouper.detect.classify import (
    Grounding,
    RecoveryClass,
    classify,
    coverage_report,
)
from recouper.providers.entities import Payment


def make_payment(
    *,
    reason: str | None = "payment_failed",
    status: str = "failed",
    reusable: bool = True,
    amount: int = 250_000,
) -> Payment:
    return Payment(
        id="pay_TEST0000000001",
        entity="payment",
        amount=amount,
        currency="INR",
        status=status,  # type: ignore[arg-type]
        order_id="order_TEST000000001",
        method="card",
        captured=False,
        email="buyer@example.com",
        contact="+919000000000",
        created_at=1_756_000_000,
        error_reason=reason,
        has_reusable_instrument=reusable,
    )


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("reason", ["stolen_or_lost_card", "fraudulent", "suspected_fraud"])
def test_fraud_reasons_are_terminal_and_forbid_contact(reason):
    """A fraud-flagged failure must stop the workflow dead.

    The person on the other end of a stolen card is probably a victim. Both
    retry and contact have to be off the table -- not merely discouraged.
    """
    c = classify(make_payment(reason=reason))
    assert c.recovery_class is RecoveryClass.TERMINAL
    assert c.can_retry_silently is False
    assert c.max_retries == 0
    assert c.allows_contact is False
    assert c.is_actionable is False


@pytest.mark.parametrize("reason", ["card_expired", "invalid_card", "card_disabled"])
def test_dead_instruments_are_never_retried_but_may_be_contacted(reason):
    """Retrying an expired card has a 0% success rate.

    Contact is still allowed, because the customer can supply a new
    instrument -- that is the only path that actually recovers this money.
    """
    c = classify(make_payment(reason=reason))
    assert c.recovery_class is RecoveryClass.INSTRUMENT_DEAD
    assert c.can_retry_silently is False
    assert c.max_retries == 0
    assert c.allows_contact is True


def test_unknown_reason_lands_in_exception_list_not_a_guess():
    """An unrecognised reason must produce no automated action at all."""
    c = classify(make_payment(reason="some_reason_we_have_never_seen"))
    assert c.recovery_class is RecoveryClass.UNCLASSIFIED
    assert c.can_retry_silently is False
    assert c.allows_contact is False
    assert c.is_actionable is False


def test_missing_error_reason_is_also_unclassified():
    c = classify(make_payment(reason=None))
    assert c.recovery_class is RecoveryClass.UNCLASSIFIED


def test_customer_cancellation_gets_at_most_a_soft_nudge():
    """A customer who cancelled said no. We do not retry a 'no'."""
    c = classify(make_payment(reason="payment_cancelled"))
    assert c.recovery_class is RecoveryClass.INTENT_NEGATIVE
    assert c.can_retry_silently is False
    assert c.allows_contact is True


# --- the two-condition retry gate ------------------------------------------


def test_retryable_class_without_instrument_cannot_be_retried():
    """The core distinction: retryable *in principle* is not retryable *by us*.

    insufficient_funds is a textbook deferred-retry case, but with no stored
    mandate there is nothing to charge. Anything else would be a phantom
    retry against an instrument we do not hold.
    """
    c = classify(make_payment(reason="insufficient_funds", reusable=False))
    assert c.recovery_class is RecoveryClass.DEFERRED
    assert c.can_retry_silently is False
    assert c.max_retries == 0
    assert c.retry_delays_s == ()
    # Contact remains open -- a payment link works without a stored instrument.
    assert c.allows_contact is True


def test_insufficient_funds_with_instrument_defers_across_a_pay_cycle():
    """Retry timing must straddle a salary date, not hammer immediately."""
    c = classify(make_payment(reason="insufficient_funds", reusable=True))
    assert c.can_retry_silently is True
    assert c.max_retries == 2
    assert c.retry_delays_s == (3 * 86400, 7 * 86400)
    assert min(c.retry_delays_s) >= 86400, "must not retry same-day"


def test_transient_failures_retry_quickly():
    c = classify(make_payment(reason="gateway_error"))
    assert c.recovery_class is RecoveryClass.TRANSIENT
    assert c.can_retry_silently is True
    assert c.retry_delays_s[0] <= 300, "transient retry should be prompt"


def test_reauth_never_retries_silently_even_with_a_mandate():
    """Authentication needs the customer present -- a mandate cannot fake it."""
    c = classify(make_payment(reason="invalid_otp", reusable=True))
    assert c.recovery_class is RecoveryClass.REAUTH
    assert c.can_retry_silently is False
    assert c.allows_contact is True


# --- hygiene ----------------------------------------------------------------


def test_classify_rejects_non_failed_payments():
    with pytest.raises(ValueError, match="failed payment"):
        classify(make_payment(status="captured"))


def test_invalid_otp_mapping_is_documentation_grounded():
    """This one reason is pinned to a published example; keep it honest."""
    c = classify(make_payment(reason="invalid_otp"))
    assert c.grounding is Grounding.GROUNDED


def test_every_rule_carries_a_rationale():
    """A classification a reviewer cannot interrogate is not auditable."""
    for reason in ["payment_failed", "insufficient_funds", "card_expired"]:
        c = classify(make_payment(reason=reason))
        assert len(c.rationale) > 40, f"{reason} needs a real rationale"


def test_coverage_report_is_honest_about_inference():
    rep = coverage_report()
    assert rep["grounded"] + rep["inferred"] == rep["total_reasons"]
    assert rep["inferred"] > 0, "we should not overstate our grounding"
