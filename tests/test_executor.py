"""Tests for the executor's failure handling.

The scenario that matters most is the timeout: a call that may or may not
have landed. Getting that wrong means double-charging a customer, which is
the single worst thing a recovery system can do.
"""

import pytest

from recouper.agent.execute import (
    BatchHalted,
    BreakerState,
    CircuitBreaker,
    Executor,
    idempotency_key,
)
from recouper.audit.ledger import AuditLedger
from recouper.policy.rules import ActionType, CustomerState, DebtState, ProposedAction
from recouper.providers.mock import FaultConfig, MockRazorpayClient
from recouper.providers.protocol import TransientProviderError

NOW = 1_788_078_600


def make_action(action=ActionType.SEND_REMINDER, debt_id="pay_A", amount=250_000):
    return ProposedAction(
        action=action,
        debt=DebtState(
            debt_id=debt_id, customer_id="cust_A", amount=amount,
            class_allows_silent_retry=True, max_retries_allowed=2,
        ),
        customer=CustomerState(customer_id="cust_A"),
        now_ts=NOW,
        rationale="test",
    )


@pytest.fixture
def parts(tmp_path):
    client = MockRazorpayClient()
    ledger = AuditLedger(tmp_path / "audit.jsonl")
    ex = Executor(client=client, ledger=ledger, sleep=lambda s: None)
    return client, ledger, ex


# --- idempotency ------------------------------------------------------------


def test_same_logical_attempt_yields_the_same_key():
    """The property that prevents double-charging.

    All transport-level retries of one logical attempt must share a key, so
    a provider deduplicates them.
    """
    a = idempotency_key("pay_A", ActionType.RETRY_PAYMENT, 1)
    b = idempotency_key("pay_A", ActionType.RETRY_PAYMENT, 1)
    assert a == b


def test_different_logical_attempts_yield_different_keys():
    """Retry #2 is a genuinely new operation and must not be deduplicated
    against retry #1, or the second attempt would silently never happen."""
    a = idempotency_key("pay_A", ActionType.RETRY_PAYMENT, 1)
    b = idempotency_key("pay_A", ActionType.RETRY_PAYMENT, 2)
    assert a != b


def test_keys_are_scoped_per_case_and_action():
    assert idempotency_key("pay_A", ActionType.SEND_REMINDER, 1) != idempotency_key(
        "pay_B", ActionType.SEND_REMINDER, 1
    )
    assert idempotency_key("pay_A", ActionType.SEND_REMINDER, 1) != idempotency_key(
        "pay_A", ActionType.CREATE_PAYMENT_LINK, 1
    )


def test_a_retried_timeout_does_not_double_notify(parts):
    """The central safety property.

    The provider succeeds but the response is lost, so the executor retries.
    Because the idempotency key is unchanged, the customer must receive
    exactly one message -- not two.
    """
    client, _, ex = parts
    real_send = client.send_notification
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        result = real_send(**kwargs)  # side effect happens
        if calls["n"] == 1:
            raise TransientProviderError("timeout after the request was accepted")
        return result

    client.send_notification = flaky  # type: ignore[method-assign]

    res = ex.execute(make_action(), attempt_no=1)

    assert res.ok
    assert calls["n"] == 2, "executor should have retried"
    assert len(client.sent_notifications) == 1, "customer must be messaged once"


# --- retries and backoff ----------------------------------------------------


def test_transient_failures_are_retried_then_succeed(parts):
    client, _, ex = parts
    client.faults = FaultConfig(transient_failure_rate=1.0, healthy_calls=0, seed=1)

    calls = {"n": 0}
    original = client.faults.maybe_fail

    def fail_twice(op):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TransientProviderError("502")

    client.faults.maybe_fail = fail_twice  # type: ignore[method-assign]

    res = ex.execute(make_action())
    assert res.ok
    assert res.attempts == 3


def test_exhausted_retries_go_to_the_dead_letter_queue(parts):
    """Work that cannot complete is parked with context, never dropped."""
    client, _, ex = parts
    client.faults = FaultConfig(transient_failure_rate=1.0, seed=2)

    res = ex.execute(make_action())

    assert not res.ok
    assert res.dead_lettered
    assert len(ex.dead_letters) == 1
    dl = ex.dead_letters[0]
    assert dl.case_id == "pay_A"
    assert dl.attempts == ex.max_transport_retries
    assert dl.context["amount"] == 250_000


def test_permanent_errors_are_not_retried(parts):
    """Retrying a 4xx wastes budget and delays the dead-letter."""
    client, _, ex = parts
    res = ex.execute(make_action(ActionType.RETRY_PAYMENT, debt_id="pay_NONEXISTENT"))
    assert not res.ok
    assert res.attempts == 1, "a permanent error must not be retried"
    assert res.dead_lettered


def test_backoff_grows_and_is_jittered(parts):
    _, _, ex = parts
    exc = TransientProviderError("502")
    early = [ex._backoff(1, exc) for _ in range(50)]
    late = [ex._backoff(3, exc) for _ in range(50)]
    assert max(early) <= ex.base_delay_s
    assert max(late) <= ex.base_delay_s * 4
    assert len(set(early)) > 1, "jitter must actually vary"


def test_rate_limit_honours_the_servers_retry_after(parts):
    from recouper.providers.protocol import RateLimitError

    _, _, ex = parts
    assert ex._backoff(1, RateLimitError("429", retry_after_s=7)) == 7.0


# --- circuit breaker --------------------------------------------------------


def test_breaker_stays_closed_on_isolated_failures():
    """Two bad calls in a healthy batch are not an outage."""
    b = CircuitBreaker(threshold=0.2, window=50, min_observations=10)
    for i in range(50):
        b.record(failed=(i < 2))
    assert b.state is BreakerState.CLOSED


def test_breaker_trips_on_a_sustained_failure_rate():
    b = CircuitBreaker(threshold=0.2, window=50, min_observations=10)
    for i in range(30):
        b.record(failed=(i % 2 == 0))  # 50% failures
    assert b.is_open
    assert "threshold" in b.trip_reason


def test_breaker_will_not_trip_before_minimum_observations():
    """Guards against halting a whole batch on the first unlucky call."""
    b = CircuitBreaker(threshold=0.2, min_observations=10)
    for _ in range(5):
        b.record(failed=True)
    assert not b.is_open


def test_a_small_absolute_number_of_failures_never_trips():
    """Regression: the rate floor alone was not enough.

    With min_observations=10 and a 20% threshold, two early failures reach
    exactly 20% over a partially-filled window and halted a healthy batch.
    A rate over a nearly-empty window is noise, so an absolute failure floor
    is required as well.
    """
    b = CircuitBreaker(threshold=0.2, window=50, min_observations=10, min_failures=5)
    for i in range(12):
        b.record(failed=(i < 4))  # 4 failures, then healthy
    assert not b.is_open, "4 failures should never constitute an outage"


def test_min_failures_does_not_prevent_tripping_on_a_real_outage():
    """The floor must not make the breaker useless."""
    b = CircuitBreaker(threshold=0.2, window=50, min_observations=10, min_failures=5)
    for _ in range(20):
        b.record(failed=True)
    assert b.is_open


def test_open_breaker_halts_the_batch(parts):
    client, _, ex = parts
    ex.breaker = CircuitBreaker(threshold=0.2, min_observations=2)
    client.faults = FaultConfig(transient_failure_rate=1.0, seed=3)

    with pytest.raises(BatchHalted):
        for i in range(10):
            ex.execute(make_action(debt_id=f"pay_{i}"))


def test_breaker_does_not_close_itself(parts):
    """Automatic recovery would re-trip against a still-broken gateway,
    sending more traffic at it each cycle. Resume must be a human act."""
    _, _, ex = parts
    ex.breaker = CircuitBreaker(threshold=0.2, min_observations=2)
    for _ in range(5):
        ex.breaker.record(failed=True)
    assert ex.breaker.is_open

    for _ in range(100):
        ex.breaker.record(failed=False)
    assert ex.breaker.is_open, "breaker must not self-heal"

    ex.breaker.reset()
    assert not ex.breaker.is_open


def test_bookkeeping_actions_do_not_pollute_breaker_health(parts):
    """Escalations make no provider call, so they are not evidence the
    gateway is healthy and must not dilute the failure rate."""
    _, _, ex = parts
    res = ex.execute(make_action(ActionType.ESCALATE_TO_HUMAN))
    assert res.ok


# --- audit integration ------------------------------------------------------


def test_execution_is_recorded_and_the_chain_stays_intact(parts):
    client, ledger, ex = parts
    for i in range(5):
        ex.execute(make_action(debt_id=f"pay_{i}"))

    assert ledger.verify().ok
    events = ledger.event_counts()
    assert events["action_executed"] == 5


def test_failures_are_recorded_before_the_dead_letter(parts):
    client, ledger, ex = parts
    client.faults = FaultConfig(transient_failure_rate=1.0, seed=4)
    ex.execute(make_action())

    events = ledger.event_counts()
    assert events["action_transient_failure"] >= 1
    assert events["action_exhausted_retries"] == 1
    assert ledger.verify().ok
