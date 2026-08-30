"""Razorpay entity shapes.

These dataclasses mirror the JSON returned by the live Razorpay API so that
agent code written against the mock provider works unchanged against
`providers.live`. Fidelity rules we hold to, taken from the API reference:

  * amounts are integers in the smallest currency unit (paise), never floats
  * timestamps are epoch seconds, never datetimes
  * field names are snake_case exactly as the API returns them
  * ids carry the API's prefixes: pay_ / order_ / inv_ / plink_ / cust_

Verified against https://razorpay.com/docs/api/payments/ (endpoint list and
error object). Fields marked `# inferred` are not pinned to a doc page we
could reach; they are modelled on standard gateway semantics and are listed
in the README so a reviewer knows exactly which parts are grounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

# Documented payment lifecycle states.
PaymentStatus = Literal[
    "created",
    "authorized",
    "captured",
    "refunded",
    "failed",
]

# Documented order states.
OrderStatus = Literal["created", "attempted", "paid"]

# Documented invoice states.
InvoiceStatus = Literal[
    "draft",
    "issued",
    "partially_paid",
    "paid",
    "cancelled",
    "expired",
]

# `error.source` — documented set.
ErrorSource = Literal["customer", "gateway", "bank", "network"]


@dataclass(frozen=True)
class PaymentError:
    """The error block attached to a failed payment.

    Shape is documented: code / description / source / step / reason / metadata.
    The canonical documented example is
        code=BAD_REQUEST_ERROR, reason=invalid_otp,
        step=payment_authentication, source=customer
    """

    code: str
    description: str
    source: ErrorSource
    step: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Payment:
    id: str
    entity: str
    amount: int  # paise
    currency: str
    status: PaymentStatus
    order_id: Optional[str]
    method: str  # card | netbanking | upi | wallet | emi
    captured: bool
    email: str
    contact: str
    created_at: int  # epoch seconds
    customer_id: Optional[str] = None
    description: Optional[str] = None
    international: bool = False
    fee: Optional[int] = None
    tax: Optional[int] = None

    # Present only when status == "failed".
    error_code: Optional[str] = None
    error_description: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None
    error_reason: Optional[str] = None

    notes: dict[str, Any] = field(default_factory=dict)

    # --- non-API fields -----------------------------------------------------
    # Whether a stored instrument / mandate exists that would let us re-attempt
    # the charge without the customer present. Not a Razorpay API field; we
    # carry it because it decides whether "retry" is even a legal action.
    has_reusable_instrument: bool = False  # inferred

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Order:
    id: str
    entity: str
    amount: int  # paise
    amount_paid: int
    amount_due: int
    currency: str
    receipt: Optional[str]
    status: OrderStatus
    attempts: int
    created_at: int
    customer_id: Optional[str] = None
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_abandoned(self) -> bool:
        """Created or attempted but never paid."""
        return self.status in ("created", "attempted") and self.amount_paid == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Invoice:
    id: str
    entity: str
    customer_id: str
    order_id: Optional[str]
    status: InvoiceStatus
    amount: int  # paise
    amount_paid: int
    amount_due: int
    currency: str
    issued_at: Optional[int]
    due_by: Optional[int]  # epoch seconds
    paid_at: Optional[int]
    created_at: int
    description: Optional[str] = None
    notes: dict[str, Any] = field(default_factory=dict)

    def is_overdue(self, now: int) -> bool:
        return (
            self.status in ("issued", "partially_paid")
            and self.due_by is not None
            and now > self.due_by
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Customer:
    id: str
    entity: str
    name: str
    email: str
    contact: str
    created_at: int
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaymentLink:
    """Razorpay Payment Link — how we let a customer re-pay without us
    holding their instrument."""

    id: str
    entity: str
    amount: int
    currency: str
    status: str  # created | partially_paid | expired | cancelled | paid
    short_url: str
    reference_id: Optional[str]
    customer: dict[str, Any]
    created_at: int
    expire_by: Optional[int] = None
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
