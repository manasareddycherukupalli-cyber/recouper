"""In-memory Razorpay simulator.

Satisfies the same `RazorpayClient` Protocol as `live.py`, so agent code
cannot tell them apart. Two responsibilities beyond serving data:

**Fault injection.** Real payment infrastructure fails, and a recovery system
that has only ever been run against a healthy gateway is untested in the case
that matters most. The `FaultConfig` lets a run inject 5xx bursts, timeouts
and rate limits deterministically, which is what drives the circuit-breaker
demonstration.

**Idempotency enforcement.** Every write records its idempotency key and
replays the original response if the same key arrives twice, exactly as a
real payment API does. This is not decoration: the executor retries on
transient failure, so without it a retried call after a *timeout* -- where
the request may well have succeeded -- would double-charge a customer. The
mock enforcing it is what makes the executor's correctness testable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Optional

from .entities import Invoice, Order, Payment, PaymentLink
from .protocol import (
    PermanentProviderError,
    RateLimitError,
    TransientProviderError,
)


@dataclass
class FaultConfig:
    """Deterministic fault injection.

    Faults begin only after `healthy_calls` successful calls so a run has a
    clean opening stretch -- a breaker that trips on call one demonstrates
    nothing about detecting degradation.
    """

    transient_failure_rate: float = 0.0
    rate_limit_rate: float = 0.0
    healthy_calls: int = 0
    seed: int = 7

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._calls = 0

    def maybe_fail(self, operation: str) -> None:
        self._calls += 1
        if self._calls <= self.healthy_calls:
            return
        roll = self._rng.random()
        if roll < self.transient_failure_rate:
            raise TransientProviderError(
                f"gateway returned 502 during {operation}"
            )
        if roll < self.transient_failure_rate + self.rate_limit_rate:
            raise RateLimitError(f"429 rate limited during {operation}", retry_after_s=2)


@dataclass
class MockRazorpayClient:
    payments: dict[str, Payment] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    invoices: dict[str, Invoice] = field(default_factory=dict)
    faults: FaultConfig = field(default_factory=FaultConfig)

    # idempotency_key -> the response we returned the first time
    _idempotency: dict[str, Any] = field(default_factory=dict)
    # Everything the system "sent", for inspection. Never leaves the process.
    sent_notifications: list[dict] = field(default_factory=list)
    created_links: list[PaymentLink] = field(default_factory=list)
    retry_attempts: list[str] = field(default_factory=list)

    _counter: int = 0

    @classmethod
    def from_corpus(cls, corpus, faults: Optional[FaultConfig] = None):
        return cls(
            payments={p.id: p for p in corpus.payments},
            orders={o.id: o for o in corpus.orders},
            invoices={i.id: i for i in corpus.invoices},
            faults=faults or FaultConfig(),
        )

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}MOCK{self._counter:010d}"

    # --- reads --------------------------------------------------------------

    def fetch_payments(self, *, from_ts=None, to_ts=None, count=100, skip=0):
        self.faults.maybe_fail("fetch_payments")
        rows = sorted(self.payments.values(), key=lambda p: p.created_at, reverse=True)
        rows = self._window(rows, from_ts, to_ts, lambda p: p.created_at)
        return rows[skip : skip + count]

    def fetch_payment(self, payment_id: str) -> Payment:
        self.faults.maybe_fail("fetch_payment")
        p = self.payments.get(payment_id)
        if p is None:
            raise PermanentProviderError(f"no such payment: {payment_id}")
        return p

    def fetch_orders(self, *, from_ts=None, to_ts=None, count=100, skip=0):
        self.faults.maybe_fail("fetch_orders")
        rows = sorted(self.orders.values(), key=lambda o: o.created_at, reverse=True)
        rows = self._window(rows, from_ts, to_ts, lambda o: o.created_at)
        return rows[skip : skip + count]

    def fetch_payments_for_order(self, order_id: str) -> list[Payment]:
        self.faults.maybe_fail("fetch_payments_for_order")
        return [p for p in self.payments.values() if p.order_id == order_id]

    def fetch_invoices(self, *, from_ts=None, to_ts=None, count=100, skip=0):
        self.faults.maybe_fail("fetch_invoices")
        rows = sorted(self.invoices.values(), key=lambda i: i.created_at, reverse=True)
        rows = self._window(rows, from_ts, to_ts, lambda i: i.created_at)
        return rows[skip : skip + count]

    @staticmethod
    def _window(rows, from_ts, to_ts, key):
        if from_ts is not None:
            rows = [r for r in rows if key(r) >= from_ts]
        if to_ts is not None:
            rows = [r for r in rows if key(r) <= to_ts]
        return rows

    # --- writes -------------------------------------------------------------

    def retry_payment(self, payment_id: str, *, idempotency_key: str) -> Payment:
        if idempotency_key in self._idempotency:
            return self._idempotency[idempotency_key]

        original = self.payments.get(payment_id)
        if original is None:
            raise PermanentProviderError(f"no such payment: {payment_id}")
        if not original.has_reusable_instrument:
            # A real gateway would reject this too. Enforced here so that a
            # bug in the policy layer surfaces as a loud failure rather than
            # a silent phantom charge.
            raise PermanentProviderError(
                f"{payment_id} has no reusable instrument to charge"
            )

        self.faults.maybe_fail("retry_payment")
        self.retry_attempts.append(payment_id)

        # The retry creates a NEW payment, as it does on the real API. The
        # outcome (captured vs failed again) is decided by the outcome
        # simulator, not here -- this layer only models the API surface.
        new = Payment(
            id=self._next_id("pay_"),
            entity="payment",
            amount=original.amount,
            currency=original.currency,
            status="created",
            order_id=original.order_id,
            method=original.method,
            captured=False,
            email=original.email,
            contact=original.contact,
            created_at=original.created_at,
            customer_id=original.customer_id,
            has_reusable_instrument=True,
            notes={"retry_of": payment_id},
        )
        self.payments[new.id] = new
        self._idempotency[idempotency_key] = new
        return new

    def create_payment_link(
        self, *, amount, currency, customer, reference_id, description,
        expire_by=None, idempotency_key: str,
    ) -> PaymentLink:
        if idempotency_key in self._idempotency:
            return self._idempotency[idempotency_key]

        self.faults.maybe_fail("create_payment_link")
        link = PaymentLink(
            id=self._next_id("plink_"),
            entity="payment_link",
            amount=amount,
            currency=currency,
            status="created",
            short_url=f"https://rzp.io/i/{self._counter:08d}",
            reference_id=reference_id,
            customer=customer,
            created_at=0,
            expire_by=expire_by,
        )
        self.created_links.append(link)
        self._idempotency[idempotency_key] = link
        return link

    def send_notification(
        self, *, channel, to, subject, body, reference_id, idempotency_key: str
    ) -> dict:
        """A logged no-op. No message leaves this process, ever.

        The recorded entry is what the metrics layer counts as a customer
        contact, and what the false-positive cost is computed from.
        """
        if idempotency_key in self._idempotency:
            return self._idempotency[idempotency_key]

        self.faults.maybe_fail("send_notification")
        entry = {
            "channel": channel,
            "to": to,
            "subject": subject,
            "body": body,
            "reference_id": reference_id,
            "delivered": True,
            "simulated": True,
        }
        self.sent_notifications.append(entry)
        self._idempotency[idempotency_key] = entry
        return entry

    # --- inspection ---------------------------------------------------------

    def contact_count(self) -> int:
        return len(self.sent_notifications) + len(self.created_links)
