"""Case extraction: turning raw provider data into recovery cases.

A "case" is one unit of at-risk money we might work. Building them correctly
is less trivial than it looks, and the subtlety is deduplication.

An order carrying a failed payment satisfies BOTH "failed payment" and
"abandoned checkout" -- its status is `attempted` and `amount_paid` is 0. The
first version of this pipeline counted such orders twice, which inflated the
case count by roughly 150 on a 955-record corpus. Nothing crashed; the
recovery rate simply had a larger denominator and every reported figure was
quietly wrong.

So extraction applies explicit precedence:

  1. A failed payment is a failed-payment case.
  2. An order is an abandoned-checkout case ONLY if no payment was ever
     attempted against it.
  3. An overdue invoice is a receivable case.

`Order.is_abandoned` is left alone -- the property is telling the truth about
an order. The deduplication belongs here, where cases are built, not in the
entity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..providers.entities import Customer, Invoice, Order, Payment
from .classify import Classification, RecoveryClass, classify

_DAY = 86_400


@dataclass
class Case:
    """One unit of at-risk money."""

    case_id: str
    kind: str  # failed_payment | abandoned_checkout | overdue_invoice
    case_class: str  # recovery class, or the kind for non-payment cases
    customer_id: Optional[str]
    amount_paise: int
    age_s: int
    classification: Optional[Classification] = None
    source_id: str = ""

    # Hard-stop signals carried through to the policy layer.
    has_open_dispute: bool = False
    has_chargeback: bool = False
    was_refunded: bool = False
    is_settled: bool = False

    @property
    def is_actionable(self) -> bool:
        if self.classification is not None:
            return self.classification.is_actionable
        return True

    @property
    def expected_value_paise(self) -> float:
        """Crude prioritisation signal: amount decayed by age.

        Used only for ordering work within a batch, never for deciding
        whether an action is permitted -- that is the policy engine's job.
        Keeping value out of the permission path is deliberate: a system that
        relaxes its rules for valuable debts has no rules.
        """
        decay = 0.5 ** (self.age_s / (21 * _DAY))
        return self.amount_paise * decay

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "case_class": self.case_class,
            "customer_id": self.customer_id,
            "amount_paise": self.amount_paise,
            "age_days": round(self.age_s / _DAY, 1),
            "actionable": self.is_actionable,
            "classification": (
                self.classification.to_dict() if self.classification else None
            ),
        }


@dataclass
class ExtractionReport:
    """What extraction found, including what it deliberately skipped.

    The skip counts are reported rather than discarded so that the case count
    is auditable: a reviewer can check that cases + skips accounts for every
    record in the corpus.
    """

    cases: list[Case]
    skipped_orders_with_payments: int = 0
    skipped_paid: int = 0
    unclassified: int = 0
    terminal: int = 0

    def summary(self) -> dict:
        by_kind: dict[str, int] = {}
        for c in self.cases:
            by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
        return {
            "total_cases": len(self.cases),
            "by_kind": by_kind,
            "actionable": sum(1 for c in self.cases if c.is_actionable),
            "unclassified_exceptions": self.unclassified,
            "terminal_hard_stops": self.terminal,
            "skipped_orders_already_counted_as_payments": (
                self.skipped_orders_with_payments
            ),
            "skipped_already_paid": self.skipped_paid,
        }


def extract_cases(
    *,
    payments: list[Payment],
    orders: list[Order],
    invoices: list[Invoice],
    customers: list[Customer],
    now: int,
) -> ExtractionReport:
    report = ExtractionReport(cases=[])

    # Which orders already have a payment attempt against them. Built once,
    # up front -- this set is the deduplication.
    orders_with_payments = {p.order_id for p in payments if p.order_id}

    # 1. Failed payments.
    for p in payments:
        if not p.is_failed:
            report.skipped_paid += 1
            continue
        cls = classify(p)
        if cls.recovery_class is RecoveryClass.UNCLASSIFIED:
            report.unclassified += 1
        if cls.recovery_class is RecoveryClass.TERMINAL:
            report.terminal += 1
        report.cases.append(
            Case(
                case_id=p.id,
                kind="failed_payment",
                case_class=cls.recovery_class.value,
                customer_id=p.customer_id,
                amount_paise=p.amount,
                age_s=max(0, now - p.created_at),
                classification=cls,
                source_id=p.id,
            )
        )

    # 2. Abandoned checkouts -- only where no payment was ever attempted.
    for o in orders:
        if not o.is_abandoned:
            report.skipped_paid += 1
            continue
        if o.id in orders_with_payments:
            # Already represented by its failed payment. Counting it here
            # too would double-count the same money.
            report.skipped_orders_with_payments += 1
            continue
        report.cases.append(
            Case(
                case_id=o.id,
                kind="abandoned_checkout",
                case_class="abandoned_cart",
                customer_id=o.customer_id,
                amount_paise=o.amount_due,
                age_s=max(0, now - o.created_at),
                source_id=o.id,
            )
        )

    # 3. Overdue receivables.
    for inv in invoices:
        if not inv.is_overdue(now):
            report.skipped_paid += 1
            continue
        report.cases.append(
            Case(
                case_id=inv.id,
                kind="overdue_invoice",
                case_class="overdue_invoice",
                customer_id=inv.customer_id,
                amount_paise=inv.amount_due,
                age_s=max(0, now - (inv.due_by or inv.created_at)),
                source_id=inv.id,
            )
        )

    # Work the most valuable cases first. Batch budgets are finite, so if we
    # run out of budget it should be on the small debts.
    report.cases.sort(key=lambda c: -c.expected_value_paise)
    return report
