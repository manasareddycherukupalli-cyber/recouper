"""Seeded synthetic corpus.

Everything here is fabricated. No real customer, card, or transaction data is
used anywhere in this project. Names and contacts are drawn from fixed
placeholder pools.

The corpus is generated from an explicit seed so that every metric in
METRICS.md is reproducible: same seed, same dataset, same numbers. A reviewer
can clone the repo and regenerate the exact batch the results were computed
on.

Distribution choices below are stated with their reasoning rather than tuned
until the results looked good. Where a figure is a guess, it says so. The
point of this project is a measurement framework that survives scrutiny; a
corpus secretly rigged to produce a flattering recovery number would defeat
it entirely.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from ..providers.entities import Customer, Invoice, Order, Payment

_DAY = 86_400

# A fixed "now" so runs are deterministic across days. 2026-08-30T00:00:00Z.
DEFAULT_NOW = 1_756_512_000


# Failure-reason mix for failed payments.
#
# Rationale: card-issuer declines (insufficient funds, expired) dominate real
# failed-payment tails, authentication drop-off is the next largest bucket,
# and infrastructure errors are comparatively rare. The `unknown_reason_*`
# entries exist on purpose -- a real feed always contains reasons your
# taxonomy has not seen, and a system that never encounters one is not being
# tested honestly. These weights are ESTIMATES, not measured from production
# data; they are declared here so a reviewer can re-weight and re-run.
FAILURE_REASON_WEIGHTS: dict[str, float] = {
    "insufficient_funds": 0.26,
    "invalid_otp": 0.17,
    "payment_cancelled": 0.13,
    "card_expired": 0.09,
    "payment_failed": 0.08,
    "gateway_error": 0.06,
    "payment_timeout": 0.05,
    "invalid_card": 0.04,
    "authentication_failed": 0.04,
    "card_disabled": 0.02,
    "payment_limit_exceeded": 0.02,
    "account_blocked": 0.01,
    "stolen_or_lost_card": 0.01,
    "suspected_fraud": 0.01,
    # Deliberately outside the taxonomy -- exercises the exception path.
    "unknown_reason_bank_ref_401": 0.005,
    "issuer_unavailable": 0.005,
}

# Which reasons plausibly leave us holding a reusable mandate/token.
# Authentication and fraud failures generally do not produce a stored
# instrument we may re-charge; a card on file that later expires does.
_REUSABLE_INSTRUMENT_LIKELIHOOD: dict[str, float] = {
    "insufficient_funds": 0.75,
    "payment_limit_exceeded": 0.70,
    "gateway_error": 0.65,
    "payment_failed": 0.60,
    "card_expired": 0.55,
    "card_disabled": 0.40,
    "invalid_card": 0.15,
    "account_blocked": 0.20,
    "invalid_otp": 0.10,
    "authentication_failed": 0.10,
    "payment_cancelled": 0.10,
    "payment_timeout": 0.10,
    "stolen_or_lost_card": 0.0,
    "suspected_fraud": 0.0,
    "fraudulent": 0.0,
}

_ERROR_SOURCE: dict[str, str] = {
    "insufficient_funds": "bank",
    "payment_limit_exceeded": "bank",
    "card_expired": "bank",
    "invalid_card": "bank",
    "card_disabled": "bank",
    "account_blocked": "bank",
    "stolen_or_lost_card": "bank",
    "suspected_fraud": "bank",
    "invalid_otp": "customer",
    "authentication_failed": "customer",
    "payment_cancelled": "customer",
    "payment_timeout": "customer",
    "gateway_error": "gateway",
    "issuer_unavailable": "gateway",
    "payment_failed": "network",
    "unknown_reason_bank_ref_401": "bank",
}

_ERROR_STEP: dict[str, str] = {
    "invalid_otp": "payment_authentication",
    "authentication_failed": "payment_authentication",
    "payment_timeout": "payment_authentication",
    "payment_cancelled": "payment_authentication",
}

_METHOD_WEIGHTS = {"upi": 0.42, "card": 0.31, "netbanking": 0.16, "wallet": 0.11}

_FIRST = [
    "Aarav", "Diya", "Vihaan", "Ananya", "Kabir", "Ishita", "Arjun", "Meera",
    "Rohan", "Saanvi", "Aditya", "Nisha", "Karthik", "Priya", "Rahul", "Tara",
    "Vikram", "Leela", "Sameer", "Anjali",
]
_LAST = [
    "Sharma", "Reddy", "Iyer", "Nair", "Gupta", "Patel", "Bose", "Menon",
    "Rao", "Kulkarni", "Chatterjee", "Desai",
]


@dataclass
class Corpus:
    """A generated dataset plus the seed that produced it."""

    seed: int
    now: int
    customers: list[Customer]
    payments: list[Payment]
    orders: list[Order]
    invoices: list[Invoice]

    def summary(self) -> dict[str, int]:
        failed = [p for p in self.payments if p.status == "failed"]
        return {
            "customers": len(self.customers),
            "payments": len(self.payments),
            "failed_payments": len(failed),
            "orders": len(self.orders),
            "abandoned_orders": sum(1 for o in self.orders if o.is_abandoned),
            "invoices": len(self.invoices),
            "overdue_invoices": sum(1 for i in self.invoices if i.is_overdue(self.now)),
            "total_records": (
                len(self.customers) + len(self.payments)
                + len(self.orders) + len(self.invoices)
            ),
        }


def _rid(rng: random.Random, prefix: str) -> str:
    """Razorpay-style id: prefix + 14 alphanumerics."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return prefix + "".join(rng.choice(alphabet) for _ in range(14))


def _amount(rng: random.Random) -> int:
    """Amount in paise, log-normal-ish.

    Real order values are heavily right-skewed: many small, few large. A
    uniform distribution would understate the tail, and the tail is exactly
    where the >Rs.50,000 human-escalation rule needs to be exercised.
    """
    base = rng.lognormvariate(mu=7.6, sigma=1.15)  # rupees
    rupees = max(49.0, min(base, 250_000.0))
    return int(round(rupees)) * 100


def _weighted(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def generate(
    *,
    seed: int = 42,
    now: int = DEFAULT_NOW,
    n_customers: int = 120,
    n_failed_payments: int = 180,
    n_abandoned_orders: int = 90,
    n_overdue_invoices: int = 70,
    n_successful_payments: int = 140,
) -> Corpus:
    """Build a reproducible corpus.

    Defaults produce ~600 records, comfortably above the 50+ bar the sibling
    finance track sets, and enough that a 20% control arm still leaves cells
    large enough for a bootstrap CI to say something non-vacuous.
    """
    rng = random.Random(seed)

    customers = _make_customers(rng, n_customers, now)
    payments: list[Payment] = []
    orders: list[Order] = []
    invoices: list[Invoice] = []

    # Successful payments: the healthy baseline traffic. Present so that
    # detector precision is measurable -- a detector only evaluated on known
    # failures cannot have a false-positive rate.
    for _ in range(n_successful_payments):
        cust = rng.choice(customers)
        amount = _amount(rng)
        created = now - rng.randint(1 * _DAY, 45 * _DAY)
        order = _make_order(rng, cust, amount, created, paid=True)
        orders.append(order)
        payments.append(
            _make_payment(rng, cust, amount, created, order.id, status="captured")
        )

    # Failed payments.
    for _ in range(n_failed_payments):
        cust = rng.choice(customers)
        amount = _amount(rng)
        created = now - rng.randint(1 * _DAY, 30 * _DAY)
        order = _make_order(rng, cust, amount, created, paid=False, attempts=1)
        orders.append(order)
        reason = _weighted(rng, FAILURE_REASON_WEIGHTS)
        payments.append(
            _make_payment(
                rng, cust, amount, created, order.id,
                status="failed", reason=reason,
            )
        )

    # Abandoned checkouts: an order exists, no payment was ever attempted.
    for _ in range(n_abandoned_orders):
        cust = rng.choice(customers)
        amount = _amount(rng)
        created = now - rng.randint(1 * _DAY, 21 * _DAY)
        orders.append(_make_order(rng, cust, amount, created, paid=False, attempts=0))

    # Overdue receivables.
    for _ in range(n_overdue_invoices):
        cust = rng.choice(customers)
        invoices.append(_make_invoice(rng, cust, now, overdue=True))

    # A control mass of healthy invoices, so "overdue" is a real signal
    # rather than a tautology of the dataset.
    for _ in range(n_overdue_invoices // 2):
        cust = rng.choice(customers)
        invoices.append(_make_invoice(rng, cust, now, overdue=False))

    rng.shuffle(payments)
    rng.shuffle(orders)
    rng.shuffle(invoices)

    return Corpus(
        seed=seed, now=now, customers=customers,
        payments=payments, orders=orders, invoices=invoices,
    )


def _make_customers(rng: random.Random, n: int, now: int) -> list[Customer]:
    out = []
    for i in range(n):
        first, last = rng.choice(_FIRST), rng.choice(_LAST)
        out.append(
            Customer(
                id=_rid(rng, "cust_"),
                entity="customer",
                name=f"{first} {last}",
                # example.invalid is reserved by RFC 2606 -- it can never
                # route to a real inbox even if this data escaped the sandbox.
                email=f"{first.lower()}.{last.lower()}{i}@example.invalid",
                contact=f"+9190{rng.randint(10_000_000, 99_999_999)}",
                created_at=now - rng.randint(30 * _DAY, 900 * _DAY),
                # A do-not-contact flag on ~4% of the book. The policy engine
                # must honour this absolutely; the corpus has to contain some
                # or that rule is never actually exercised.
                notes={"do_not_contact": rng.random() < 0.04},
            )
        )
    return out


def _make_order(
    rng: random.Random,
    cust: Customer,
    amount: int,
    created: int,
    *,
    paid: bool,
    attempts: int = 0,
) -> Order:
    return Order(
        id=_rid(rng, "order_"),
        entity="order",
        amount=amount,
        amount_paid=amount if paid else 0,
        amount_due=0 if paid else amount,
        currency="INR",
        receipt=f"rcpt_{rng.randint(100000, 999999)}",
        status="paid" if paid else ("attempted" if attempts else "created"),
        attempts=attempts,
        created_at=created,
        customer_id=cust.id,
    )


def _make_payment(
    rng: random.Random,
    cust: Customer,
    amount: int,
    created: int,
    order_id: str,
    *,
    status: str,
    reason: Optional[str] = None,
) -> Payment:
    method = _weighted(rng, _METHOD_WEIGHTS)
    p = Payment(
        id=_rid(rng, "pay_"),
        entity="payment",
        amount=amount,
        currency="INR",
        status=status,  # type: ignore[arg-type]
        order_id=order_id,
        method=method,
        captured=(status == "captured"),
        email=cust.email,
        contact=cust.contact,
        created_at=created,
        customer_id=cust.id,
    )
    if status == "failed" and reason:
        p.error_reason = reason
        p.error_code = "BAD_REQUEST_ERROR"
        p.error_source = _ERROR_SOURCE.get(reason, "bank")
        p.error_step = _ERROR_STEP.get(reason, "payment_authorization")
        p.error_description = reason.replace("_", " ").capitalize()
        # UPI has no stored-card equivalent here, so a mandate is rarer.
        likelihood = _REUSABLE_INSTRUMENT_LIKELIHOOD.get(reason, 0.3)
        if method == "upi":
            likelihood *= 0.4
        p.has_reusable_instrument = rng.random() < likelihood
    return p


def _make_invoice(
    rng: random.Random, cust: Customer, now: int, *, overdue: bool
) -> Invoice:
    amount = _amount(rng)
    if overdue:
        issued = now - rng.randint(20 * _DAY, 120 * _DAY)
        due = issued + rng.choice([7, 15, 30]) * _DAY
        if due >= now:  # force it genuinely past due
            due = now - rng.randint(1 * _DAY, 40 * _DAY)
        status, paid_at, amount_paid = "issued", None, 0
    else:
        issued = now - rng.randint(5 * _DAY, 60 * _DAY)
        due = issued + 30 * _DAY
        status, paid_at, amount_paid = "paid", issued + 3 * _DAY, amount

    return Invoice(
        id=_rid(rng, "inv_"),
        entity="invoice",
        customer_id=cust.id,
        order_id=None,
        status=status,  # type: ignore[arg-type]
        amount=amount,
        amount_paid=amount_paid,
        amount_due=amount - amount_paid,
        currency="INR",
        issued_at=issued,
        due_by=due,
        paid_at=paid_at,
        created_at=issued,
        description="Subscription renewal" if rng.random() < 0.5 else "Services rendered",
    )


if __name__ == "__main__":  # pragma: no cover
    import json

    corpus = generate()
    print(json.dumps(corpus.summary(), indent=2))
