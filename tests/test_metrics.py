"""Tests for the honest-measurement layer.

These matter more than any other tests in the project. Everything else can be
subtly wrong and produce a system that works less well; this can be subtly
wrong and produce a system that lies convincingly.

The key tests are the ones asserting we do NOT claim credit we have not
earned -- particularly `test_no_lift_when_treatment_does_nothing`.
"""

import pytest

from recouper.eval.metrics import CaseOutcome, bootstrap_lift, compute


def cases(n, *, arm, recovered_frac, case_class="deferred", amount=100_000, contacts=1):
    n_rec = int(round(n * recovered_frac))
    return [
        CaseOutcome(
            case_id=f"{arm}_{i}",
            case_class=case_class,
            arm=arm,
            amount_paise=amount,
            recovered=(i < n_rec),
            contacts_sent=contacts if arm == "treated" else 0,
            cost_paise=25 if arm == "treated" else 0,
        )
        for i in range(n)
    ]


# --- the central property ---------------------------------------------------


def test_no_lift_when_treatment_does_nothing():
    """The test this whole project exists to pass.

    Both arms recover at 40%. The agent achieved nothing. Gross recovery is
    large and impressive; the honest answer is zero, and the CI must contain
    zero so we cannot claim significance.
    """
    outcomes = cases(400, arm="treated", recovered_frac=0.40) + cases(
        100, arm="control", recovered_frac=0.40
    )
    m = compute(outcomes)

    assert m.gross_recovered_paise == 160 * 100_000, "gross looks impressive"
    assert abs(m.lift.point) < 0.02
    assert m.lift.low < 0 < m.lift.high, "CI must contain zero"
    assert not m.lift.excludes_zero, "must not claim significance"


def test_real_lift_is_detected():
    """The converse: a genuine effect must not be dismissed."""
    outcomes = cases(400, arm="treated", recovered_frac=0.60) + cases(
        100, arm="control", recovered_frac=0.30
    )
    m = compute(outcomes)

    assert m.lift.point == pytest.approx(0.30, abs=0.01)
    assert m.lift.excludes_zero
    assert m.incremental_recovered_paise > 0


def test_gross_always_exceeds_incremental_when_self_recovery_exists():
    """Quantifies the overstatement a gross-only report would make."""
    outcomes = cases(400, arm="treated", recovered_frac=0.50) + cases(
        100, arm="control", recovered_frac=0.35
    )
    m = compute(outcomes)

    assert m.gross_recovered_paise > m.incremental_recovered_paise
    assert m.overstatement_factor > 2.0
    assert m.self_recovery_rate == pytest.approx(0.35, abs=0.01)


def test_negative_lift_is_reported_not_hidden():
    """If we made things worse, the number must say so.

    Plausible in reality: badly-timed dunning can push a wavering customer
    into cancelling. A framework that clamps at zero would conceal exactly
    the finding an operator most needs.
    """
    outcomes = cases(400, arm="treated", recovered_frac=0.25) + cases(
        100, arm="control", recovered_frac=0.45
    )
    m = compute(outcomes)

    assert m.lift.point < 0
    assert m.incremental_recovered_paise < 0
    assert m.cost_per_rupee_recovered is None, "no cost-per-rupee on negative lift"


# --- confidence intervals ---------------------------------------------------


def test_small_samples_produce_wide_intervals():
    """A tiny cohort must not yield a confident claim."""
    small = compute(
        cases(20, arm="treated", recovered_frac=0.6)
        + cases(5, arm="control", recovered_frac=0.4)
    )
    large = compute(
        cases(2000, arm="treated", recovered_frac=0.6)
        + cases(500, arm="control", recovered_frac=0.4)
    )
    small_w = small.lift.high - small.lift.low
    large_w = large.lift.high - large.lift.low
    assert small_w > large_w * 3


def test_ci_brackets_the_point_estimate():
    m = compute(
        cases(300, arm="treated", recovered_frac=0.55)
        + cases(100, arm="control", recovered_frac=0.35)
    )
    assert m.lift.low <= m.lift.point <= m.lift.high


def test_bootstrap_is_deterministic_under_a_fixed_seed():
    """Reported numbers must be reproducible from the repo."""
    t = cases(200, arm="treated", recovered_frac=0.5)
    c = cases(60, arm="control", recovered_frac=0.3)
    a = bootstrap_lift(t, c, seed=7)
    b = bootstrap_lift(t, c, seed=7)
    assert (a.low, a.point, a.high) == (b.low, b.point, b.high)


def test_empty_control_arm_yields_no_claim():
    """Without a control arm there is no honest lift to report."""
    m = compute(cases(100, arm="treated", recovered_frac=0.5))
    import math
    assert math.isnan(m.lift.low)


# --- false positives --------------------------------------------------------


def test_false_positive_count_tracks_self_recovery():
    """The customers we bothered for nothing.

    With a 40% self-recovery rate, roughly 40% of everyone we contacted was
    going to pay regardless. That is the honest cost of intervening.
    """
    outcomes = cases(200, arm="treated", recovered_frac=0.6, contacts=1) + cases(
        100, arm="control", recovered_frac=0.40
    )
    m = compute(outcomes)
    assert m.wasted_contacts == pytest.approx(80, abs=5)


# --- per-class breakdown ----------------------------------------------------


def test_per_class_breakdown_separates_cohorts():
    """A blended number hides which cohorts the agent actually helps."""
    outcomes = (
        cases(200, arm="treated", recovered_frac=0.7, case_class="deferred")
        + cases(60, arm="control", recovered_frac=0.3, case_class="deferred")
        + cases(200, arm="treated", recovered_frac=0.3, case_class="intent_negative")
        + cases(60, arm="control", recovered_frac=0.3, case_class="intent_negative")
    )
    m = compute(outcomes)

    assert m.per_class["deferred"]["lift"]["point"] > 0.3
    assert abs(m.per_class["intent_negative"]["lift"]["point"]) < 0.05
    assert not m.per_class["intent_negative"]["lift"]["significant"]


def test_tiny_class_is_flagged_rather_than_estimated():
    outcomes = cases(200, arm="treated", recovered_frac=0.5, case_class="deferred") + [
        CaseOutcome("solo", "rare_class", "treated", 1000, True)
    ]
    m = compute(outcomes)
    assert "note" in m.per_class["rare_class"]


# --- reporting contract -----------------------------------------------------


def test_gross_is_labelled_as_inflated_in_the_output():
    """The label travels with the number.

    A reader who sees only the JSON must not be able to mistake gross
    recovery for a claim of impact.
    """
    m = compute(
        cases(100, arm="treated", recovered_frac=0.5)
        + cases(30, arm="control", recovered_frac=0.4)
    )
    d = m.to_dict()
    assert "INFLATED" in d["money_paise"]["gross_recovered_note"]
    assert "would have paid" in d["money_paise"]["gross_recovered_note"]
