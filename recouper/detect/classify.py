"""Retryability taxonomy.

Retrying a payment that cannot succeed is not free: it burns gateway fees,
annoys the customer, and can trip issuer velocity limits that damage the
merchant's approval rate on *good* traffic. So the first question is never
"how do we recover this?" but "is this recoverable at all, and by what means?"

This module is deliberately a deterministic lookup table rather than an LLM
call. Classification decides whether we are allowed to touch a customer's
money; that decision has to be auditable, unit-testable, and identical on
every run. An LLM belongs downstream, choosing *between already-legal*
actions -- see agent/plan.py.

Grounding: the error object shape (code / description / source / step /
reason / metadata, with source in {customer, gateway, bank, network}) and the
`invalid_otp` example are documented in the Razorpay API reference. The full
catalogue of `reason` strings is not reachable in the public docs we could
fetch, so entries below are marked GROUNDED or INFERRED. Anything arriving at
runtime that we do not recognise is NOT guessed at -- it lands in
UNCLASSIFIED and goes on the exception list.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from ..providers.entities import Payment


class RecoveryClass(enum.Enum):
    """What kind of recovery, if any, this failure admits."""

    TRANSIENT = "transient"
    """Nothing is wrong with the customer or the instrument -- the request
    lost a race with infrastructure. Immediate retry is appropriate."""

    DEFERRED = "deferred"
    """The instrument is valid but currently cannot fund the charge. Retrying
    now fails again; retrying later may succeed. Time is the remedy."""

    REAUTH = "reauth"
    """Authentication did not complete. By definition this needs the customer
    present, so a silent retry is impossible -- we must ask them to return."""

    INSTRUMENT_DEAD = "instrument_dead"
    """The stored instrument can never succeed again. Retrying has a 0%
    success rate. We need a different instrument from the customer."""

    INTENT_NEGATIVE = "intent_negative"
    """The customer actively declined. They may have changed their mind, but
    pressure here is both ineffective and hostile. One soft nudge, then stop."""

    TERMINAL = "terminal"
    """Fraud, stolen card, blocked account. Not only unrecoverable -- actively
    harmful to touch. No retry, no contact, route to risk."""

    UNCLASSIFIED = "unclassified"
    """We do not recognise this failure reason. We take no automated action
    and surface it on the exception list. Guessing here would be the
    dishonest option; an explicit unknown is the credible one."""


class Grounding(enum.Enum):
    """Whether a mapping is pinned to published documentation."""

    GROUNDED = "grounded"
    INFERRED = "inferred"


@dataclass(frozen=True)
class ClassificationRule:
    recovery_class: RecoveryClass
    grounding: Grounding
    rationale: str
    max_retries: int = 0
    retry_delays_s: tuple[int, ...] = ()
    allows_contact: bool = True


_HOUR = 3600
_DAY = 24 * _HOUR


# The table. Keys are `error_reason` values as returned by the API.
RULES: dict[str, ClassificationRule] = {
    # -- transient ----------------------------------------------------------
    "payment_failed": ClassificationRule(
        RecoveryClass.TRANSIENT,
        Grounding.INFERRED,
        "Generic failure most often surfaced with source=network; the "
        "instrument itself is unimpeached, so an immediate re-attempt is "
        "reasonable. Capped at 2 so a systemic outage cannot become a storm.",
        max_retries=2,
        retry_delays_s=(30, 5 * 60),
    ),
    "gateway_error": ClassificationRule(
        RecoveryClass.TRANSIENT,
        Grounding.INFERRED,
        "Failure originated inside the gateway, not with the customer. Retry "
        "with backoff; the circuit breaker handles the case where this is "
        "not isolated but an outage.",
        max_retries=2,
        retry_delays_s=(60, 10 * 60),
    ),
    "server_error": ClassificationRule(
        RecoveryClass.TRANSIENT,
        Grounding.INFERRED,
        "Upstream 5xx. Same treatment as gateway_error.",
        max_retries=2,
        retry_delays_s=(60, 10 * 60),
    ),
    # -- deferred -----------------------------------------------------------
    "insufficient_funds": ClassificationRule(
        RecoveryClass.DEFERRED,
        Grounding.INFERRED,
        "Account balance is time-dependent and correlates with salary cycles. "
        "Retrying within minutes fails again and wastes a gateway attempt; "
        "T+3d then T+7d straddles a typical pay date without becoming "
        "harassment.",
        max_retries=2,
        retry_delays_s=(3 * _DAY, 7 * _DAY),
    ),
    "payment_limit_exceeded": ClassificationRule(
        RecoveryClass.DEFERRED,
        Grounding.INFERRED,
        "Per-transaction or daily cap hit. Caps reset, so a next-day retry is "
        "meaningfully different from an immediate one.",
        max_retries=1,
        retry_delays_s=(1 * _DAY,),
    ),
    # -- reauth -------------------------------------------------------------
    "invalid_otp": ClassificationRule(
        RecoveryClass.REAUTH,
        Grounding.GROUNDED,
        "Documented example in the API reference: BAD_REQUEST_ERROR / "
        "invalid_otp / step=payment_authentication / source=customer. "
        "Authentication requires the customer present, so no silent retry is "
        "possible -- we can only invite them back via a payment link.",
        max_retries=0,
    ),
    "payment_authentication_failed": ClassificationRule(
        RecoveryClass.REAUTH,
        Grounding.INFERRED,
        "3-D Secure / step-up authentication did not complete. Needs the "
        "customer, same as invalid_otp.",
        max_retries=0,
    ),
    "authentication_failed": ClassificationRule(
        RecoveryClass.REAUTH,
        Grounding.INFERRED,
        "Alias observed for authentication step failures.",
        max_retries=0,
    ),
    # -- instrument dead ----------------------------------------------------
    "card_expired": ClassificationRule(
        RecoveryClass.INSTRUMENT_DEAD,
        Grounding.INFERRED,
        "An expired card cannot be revived by retrying -- success rate is "
        "exactly zero. The only recovery path is a new instrument, which "
        "means asking the customer.",
        max_retries=0,
    ),
    "invalid_card": ClassificationRule(
        RecoveryClass.INSTRUMENT_DEAD,
        Grounding.INFERRED,
        "Card details rejected outright by the issuer. Retrying identical "
        "details is pointless.",
        max_retries=0,
    ),
    "card_disabled": ClassificationRule(
        RecoveryClass.INSTRUMENT_DEAD,
        Grounding.INFERRED,
        "Issuer has disabled the instrument (often online/international use). "
        "Needs customer action with their bank, not a retry.",
        max_retries=0,
    ),
    "account_blocked": ClassificationRule(
        RecoveryClass.INSTRUMENT_DEAD,
        Grounding.INFERRED,
        "Underlying account is blocked. No retry can succeed; contact is "
        "still permitted so the customer can choose another method.",
        max_retries=0,
    ),
    # -- intent negative ----------------------------------------------------
    "payment_cancelled": ClassificationRule(
        RecoveryClass.INTENT_NEGATIVE,
        Grounding.INFERRED,
        "The customer affirmatively abandoned the payment. Treating a 'no' as "
        "a retryable error is how recovery systems become spam. One soft "
        "reminder is defensible; a sequence is not.",
        max_retries=0,
    ),
    "payment_timeout": ClassificationRule(
        RecoveryClass.INTENT_NEGATIVE,
        Grounding.INFERRED,
        "Customer walked away mid-flow. Ambiguous between distraction and "
        "decline, so we treat it as weak-negative intent: one nudge only.",
        max_retries=0,
    ),
    # -- terminal -----------------------------------------------------------
    "stolen_or_lost_card": ClassificationRule(
        RecoveryClass.TERMINAL,
        Grounding.INFERRED,
        "The cardholder we would be contacting is very likely a victim, not "
        "our debtor. Contact is actively harmful. Hard stop, route to risk.",
        max_retries=0,
        allows_contact=False,
    ),
    "fraudulent": ClassificationRule(
        RecoveryClass.TERMINAL,
        Grounding.INFERRED,
        "Flagged as fraud upstream. Pursuing recovery would mean pursuing a "
        "transaction we have been told not to trust.",
        max_retries=0,
        allows_contact=False,
    ),
    "suspected_fraud": ClassificationRule(
        RecoveryClass.TERMINAL,
        Grounding.INFERRED,
        "Same posture as `fraudulent`: when in doubt on fraud, stop. The cost "
        "of wrongly chasing a fraud case exceeds the value of the debt.",
        max_retries=0,
        allows_contact=False,
    ),
}


UNCLASSIFIED_RULE = ClassificationRule(
    RecoveryClass.UNCLASSIFIED,
    Grounding.GROUNDED,
    "Failure reason not present in the taxonomy. No automated action is "
    "taken; the case is surfaced on the exception list for a human. We would "
    "rather report a known gap than act on a guess.",
    max_retries=0,
    allows_contact=False,
)


@dataclass(frozen=True)
class Classification:
    """The verdict for one failed payment."""

    payment_id: str
    error_reason: Optional[str]
    recovery_class: RecoveryClass
    grounding: Grounding
    rationale: str
    max_retries: int
    retry_delays_s: tuple[int, ...]
    allows_contact: bool
    can_retry_silently: bool

    @property
    def is_actionable(self) -> bool:
        return self.recovery_class not in (
            RecoveryClass.TERMINAL,
            RecoveryClass.UNCLASSIFIED,
        )

    def to_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "error_reason": self.error_reason,
            "recovery_class": self.recovery_class.value,
            "grounding": self.grounding.value,
            "rationale": self.rationale,
            "max_retries": self.max_retries,
            "retry_delays_s": list(self.retry_delays_s),
            "allows_contact": self.allows_contact,
            "can_retry_silently": self.can_retry_silently,
        }


def classify(payment: Payment) -> Classification:
    """Map a failed payment to its recovery class.

    Two independent conditions must both hold before we will ever re-attempt
    a charge without the customer present:

      1. the failure class permits a silent retry at all, and
      2. a reusable instrument/mandate actually exists.

    A payment can be perfectly retryable in principle and still not be
    retryable by us, because we hold nothing to charge. Collapsing these two
    into one flag is a common source of phantom retries, so they stay
    separate.
    """
    if not payment.is_failed:
        raise ValueError(
            f"classify() expects a failed payment, got status={payment.status!r}"
        )

    reason = payment.error_reason
    rule = RULES.get(reason) if reason else None
    if rule is None:
        rule = UNCLASSIFIED_RULE

    can_retry_silently = rule.max_retries > 0 and payment.has_reusable_instrument

    return Classification(
        payment_id=payment.id,
        error_reason=reason,
        recovery_class=rule.recovery_class,
        grounding=rule.grounding,
        rationale=rule.rationale,
        max_retries=rule.max_retries if payment.has_reusable_instrument else 0,
        retry_delays_s=rule.retry_delays_s if payment.has_reusable_instrument else (),
        allows_contact=rule.allows_contact,
        can_retry_silently=can_retry_silently,
    )


def coverage_report() -> dict[str, int]:
    """How much of the taxonomy rests on documentation vs inference.

    Surfaced in the dashboard and README. A reviewer should be able to see the
    limits of our grounding without reading the source.
    """
    grounded = sum(1 for r in RULES.values() if r.grounding is Grounding.GROUNDED)
    return {
        "total_reasons": len(RULES),
        "grounded": grounded,
        "inferred": len(RULES) - grounded,
    }
