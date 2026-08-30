"""The executor: turns an allowed action into a real provider call, safely.

Four protections, each earning its place:

**Idempotency keys.** Derived deterministically from (case, action, attempt)
rather than randomly, so a retry after a *timeout* -- where we genuinely do
not know whether the first call landed -- reuses the same key and the
provider deduplicates it. Random keys would make every retry a fresh
operation, which is precisely how double-charging happens.

**Exponential backoff with jitter.** Without jitter, every worker retrying a
failed batch retries in lockstep and hits the recovering gateway with a
synchronised thundering herd, causing the outage it is recovering from.

**Circuit breaker.** Retries handle isolated failures. They make a *systemic*
outage worse, because every case independently discovers the gateway is down
and independently burns its retry budget. The breaker notices the pattern at
the batch level and stops.

**Dead letter queue.** An action that cannot complete is not dropped and not
retried forever -- it is parked with its full context for a human. Silent
loss is the failure mode that destroys trust in a recovery system, because
the money simply never gets chased and nobody knows.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from ..audit.ledger import AuditLedger
from ..policy.rules import ActionType, ProposedAction
from ..providers.protocol import (
    PermanentProviderError,
    ProviderError,
    RateLimitError,
    RazorpayClient,
)


class BreakerState(Enum):
    CLOSED = "closed"  # healthy, traffic flows
    OPEN = "open"      # tripped, batch halted


@dataclass
class CircuitBreaker:
    """Trips when the recent failure rate crosses a threshold.

    Rate over a sliding window rather than a raw count of consecutive
    failures: a gateway degrading to 40% errors is an outage worth stopping
    for, but it may never produce a long consecutive run, so a
    consecutive-failure breaker would never trip on it.

    Two independent floors must both be cleared before it can trip, and the
    second was added only after a test caught the first being insufficient:

    * `min_observations` -- enough calls to compute a meaningful rate at all.
    * `min_failures` -- an absolute count of failures.

    The rate floor alone is not enough. Early in a batch the window is
    partially filled, so with a 20% threshold and 10 observations, *two*
    unlucky failures reach exactly 20% and halt an otherwise healthy run. A
    rate computed over a nearly-empty window is dominated by noise. Requiring
    an absolute failure count as well means transient bad luck cannot trip
    the breaker, while a genuine outage -- which produces failures in
    quantity -- still does.
    """

    threshold: float = 0.20
    window: int = 50
    min_observations: int = 10
    min_failures: int = 5

    state: BreakerState = BreakerState.CLOSED
    _outcomes: list[bool] = field(default_factory=list)  # True == failure
    tripped_at: Optional[float] = None
    trip_reason: Optional[str] = None

    def record(self, *, failed: bool) -> None:
        self._outcomes.append(failed)
        if len(self._outcomes) > self.window:
            self._outcomes.pop(0)

        if self.state is BreakerState.OPEN:
            return
        if len(self._outcomes) < self.min_observations:
            return

        failures = sum(self._outcomes)
        if failures < self.min_failures:
            return

        rate = failures / len(self._outcomes)
        if rate >= self.threshold:
            self.state = BreakerState.OPEN
            self.tripped_at = time.time()
            self.trip_reason = (
                f"{failures} failures ({rate:.0%}) over the last "
                f"{len(self._outcomes)} actions crossed the "
                f"{self.threshold:.0%} threshold"
            )

    @property
    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN

    @property
    def failure_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(self._outcomes) / len(self._outcomes)

    def reset(self) -> None:
        """Manual resume only.

        Deliberately not automatic. A breaker that closes itself on a timer
        will happily re-trip against a gateway that is still broken, and each
        cycle sends more traffic at it. Requiring a human means somebody has
        actually looked.
        """
        self.state = BreakerState.CLOSED
        self._outcomes.clear()
        self.tripped_at = None
        self.trip_reason = None


class BatchHalted(Exception):
    """Raised when the breaker opens mid-batch."""


@dataclass
class DeadLetter:
    case_id: str
    action: str
    attempts: int
    last_error: str
    context: dict

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "action": self.action,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "context": self.context,
        }


@dataclass
class ExecutionResult:
    ok: bool
    action: str
    case_id: str
    attempts: int
    response: Optional[dict] = None
    error: Optional[str] = None
    dead_lettered: bool = False


def idempotency_key(case_id: str, action: ActionType, attempt_no: int) -> str:
    """Deterministic key for one logical operation.

    `attempt_no` is the *logical* attempt (retry #1 vs retry #2 of a payment),
    NOT the transport-level retry counter. All transport retries of the same
    logical attempt share a key -- that is the entire point. Incrementing per
    network retry would defeat deduplication exactly when it is needed.
    """
    raw = f"{case_id}:{action.value}:{attempt_no}"
    return "idem_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class Executor:
    client: RazorpayClient
    ledger: AuditLedger
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    max_transport_retries: int = 3
    base_delay_s: float = 0.5
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=lambda: random.Random(11))

    dead_letters: list[DeadLetter] = field(default_factory=list)
    actions_executed: int = 0

    def execute(
        self,
        action: ProposedAction,
        *,
        attempt_no: int = 1,
        payload: Optional[dict] = None,
    ) -> ExecutionResult:
        """Run one allowed action. Assumes the policy gate already passed."""
        if self.breaker.is_open:
            raise BatchHalted(self.breaker.trip_reason or "circuit breaker open")

        case_id = action.debt.debt_id
        key = idempotency_key(case_id, action.action, attempt_no)
        payload = payload or {}

        last_error: Optional[ProviderError] = None

        for transport_try in range(1, self.max_transport_retries + 1):
            try:
                response = self._dispatch(action, payload, key)
            except PermanentProviderError as exc:
                # Retrying cannot help. Record as a failure for breaker
                # purposes and dead-letter immediately.
                self.breaker.record(failed=True)
                self._dead_letter(action, transport_try, str(exc), payload)
                self.ledger.append(
                    actor="provider", event="action_failed_permanent",
                    case_id=case_id, action=action.action.value,
                    idempotency_key=key, reason=str(exc),
                )
                self._maybe_halt()
                return ExecutionResult(
                    False, action.action.value, case_id, transport_try,
                    error=str(exc), dead_lettered=True,
                )
            except ProviderError as exc:
                last_error = exc
                self.ledger.append(
                    actor="provider", event="action_transient_failure",
                    case_id=case_id, action=action.action.value,
                    idempotency_key=key, reason=str(exc),
                    transport_attempt=transport_try,
                )
                if transport_try < self.max_transport_retries:
                    self.sleep(self._backoff(transport_try, exc))
                continue
            else:
                self.breaker.record(failed=False)
                self.actions_executed += 1
                self.ledger.append(
                    actor="system", event="action_executed",
                    case_id=case_id, action=action.action.value,
                    decision="allow", idempotency_key=key,
                    result=self._summarise(response),
                    logical_attempt=attempt_no,
                    transport_attempts=transport_try,
                )
                return ExecutionResult(
                    True, action.action.value, case_id, transport_try,
                    response=self._summarise(response),
                )

        # Every transport retry exhausted.
        self.breaker.record(failed=True)
        msg = str(last_error) if last_error else "unknown transport failure"
        self._dead_letter(action, self.max_transport_retries, msg, payload)
        self.ledger.append(
            actor="provider", event="action_exhausted_retries",
            case_id=case_id, action=action.action.value,
            idempotency_key=key, reason=msg,
        )
        self._maybe_halt()
        return ExecutionResult(
            False, action.action.value, case_id, self.max_transport_retries,
            error=msg, dead_lettered=True,
        )

    # --- internals ----------------------------------------------------------

    def _backoff(self, attempt: int, exc: ProviderError) -> float:
        """Exponential backoff with full jitter.

        Full jitter (uniform over [0, cap]) rather than a fixed delay plus a
        small wobble: it spreads a synchronised herd far more effectively,
        which is the whole reason jitter exists.
        """
        if isinstance(exc, RateLimitError):
            # Honour the server's instruction rather than our own schedule.
            return float(exc.retry_after_s)
        cap = self.base_delay_s * (2 ** (attempt - 1))
        return self.rng.uniform(0, cap)

    def _dispatch(
        self, action: ProposedAction, payload: dict, key: str
    ) -> Any:
        a = action.action
        if a is ActionType.RETRY_PAYMENT:
            return self.client.retry_payment(
                action.debt.debt_id, idempotency_key=key
            )
        if a is ActionType.CREATE_PAYMENT_LINK:
            return self.client.create_payment_link(
                amount=action.debt.amount,
                currency=payload.get("currency", "INR"),
                customer=payload.get("customer", {}),
                reference_id=action.debt.debt_id,
                description=payload.get("description", "Complete your payment"),
                idempotency_key=key,
            )
        if a is ActionType.SEND_REMINDER:
            return self.client.send_notification(
                channel=payload.get("channel", "email"),
                to=payload.get("to", ""),
                subject=payload.get("subject", "Your payment did not go through"),
                body=payload.get("body", ""),
                reference_id=action.debt.debt_id,
                idempotency_key=key,
            )
        if a in (ActionType.ESCALATE_TO_HUMAN, ActionType.CLOSE_NO_ACTION):
            # Bookkeeping only -- no provider call, so it cannot fail and
            # must not be counted in the breaker's health statistics.
            return {"action": a.value, "queued_for_human": a is ActionType.ESCALATE_TO_HUMAN}
        raise PermanentProviderError(f"executor cannot dispatch {a}")

    def _dead_letter(
        self, action: ProposedAction, attempts: int, error: str, payload: dict
    ) -> None:
        self.dead_letters.append(
            DeadLetter(
                case_id=action.debt.debt_id,
                action=action.action.value,
                attempts=attempts,
                last_error=error,
                context={
                    "customer_id": action.debt.customer_id,
                    "amount": action.debt.amount,
                    "rationale": action.rationale,
                    "payload": payload,
                },
            )
        )

    def _maybe_halt(self) -> None:
        if self.breaker.is_open:
            self.ledger.append(
                actor="system", event="circuit_breaker_tripped",
                reason=self.breaker.trip_reason,
                failure_rate=round(self.breaker.failure_rate, 3),
                dead_letters=len(self.dead_letters),
            )
            raise BatchHalted(self.breaker.trip_reason or "circuit breaker open")

    @staticmethod
    def _summarise(response: Any) -> dict:
        if isinstance(response, dict):
            return {k: v for k, v in response.items() if k != "body"}
        for attr in ("id", "status", "short_url"):
            if hasattr(response, attr):
                return {
                    "id": getattr(response, "id", None),
                    "status": getattr(response, "status", None),
                }
        return {"result": str(response)}
