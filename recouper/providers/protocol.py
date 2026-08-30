"""The provider boundary.

This is the ONLY interface agent code is allowed to import. Nothing in
`detect/`, `policy/`, `agent/` or `eval/` may import `providers.mock` or
`providers.live` directly -- they take a `RazorpayClient` by injection.

That discipline is the whole point: swapping the in-memory simulator for real
test-mode credentials is a one-line change at the composition root, not a
refactor. `tests/test_provider_parity.py` enforces that both implementations
satisfy this Protocol.

Method names deliberately echo the official Python SDK's namespaced calls
(`client.payment.fetch_all`, `client.order.all`, ...) flattened into one
surface, so the mapping to the real SDK in `live.py` stays obvious.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .entities import Invoice, Order, Payment, PaymentLink


class ProviderError(Exception):
    """Base class for all provider-surface failures.

    Agent code catches this rather than any vendor-specific exception, so the
    executor's retry/breaker logic is provider-agnostic.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class TransientProviderError(ProviderError):
    """Gateway 5xx, timeout, connection reset -- worth retrying."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class PermanentProviderError(ProviderError):
    """4xx, validation failure, unknown id -- retrying cannot help."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class RateLimitError(TransientProviderError):
    """429. Retryable, but must respect a backoff window."""

    def __init__(self, message: str, retry_after_s: int = 1) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@runtime_checkable
class RazorpayClient(Protocol):
    """The narrow slice of Razorpay we actually depend on."""

    # --- reads --------------------------------------------------------------

    def fetch_payments(
        self,
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        count: int = 100,
        skip: int = 0,
    ) -> list[Payment]:
        """GET /v1/payments"""
        ...

    def fetch_payment(self, payment_id: str) -> Payment:
        """GET /v1/payments/:id"""
        ...

    def fetch_orders(
        self,
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        count: int = 100,
        skip: int = 0,
    ) -> list[Order]:
        """GET /v1/orders"""
        ...

    def fetch_payments_for_order(self, order_id: str) -> list[Payment]:
        """GET /v1/orders/:id/payments"""
        ...

    def fetch_invoices(
        self,
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        count: int = 100,
        skip: int = 0,
    ) -> list[Invoice]:
        """GET /v1/invoices"""
        ...

    # --- writes -------------------------------------------------------------
    # Every write takes an idempotency_key. The executor may retry any call
    # after a transient failure, and must never double-charge or double-notify
    # a customer as a result.

    def retry_payment(
        self,
        payment_id: str,
        *,
        idempotency_key: str,
    ) -> Payment:
        """Re-attempt a charge against a stored instrument.

        Only legal when the payment carries `has_reusable_instrument`; the
        policy engine enforces that before we ever get here.
        """
        ...

    def create_payment_link(
        self,
        *,
        amount: int,
        currency: str,
        customer: dict[str, Any],
        reference_id: str,
        description: str,
        expire_by: Optional[int] = None,
        idempotency_key: str,
    ) -> PaymentLink:
        """POST /v1/payment_links"""
        ...

    def send_notification(
        self,
        *,
        channel: str,  # "email" | "sms" | "whatsapp"
        to: str,
        subject: Optional[str],
        body: str,
        reference_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Deliver a nudge.

        NOTE: in this build every implementation is a logged no-op. We never
        contact a real person. The interface exists so the boundary is honest
        about what a production deployment would do here.
        """
        ...
