"""The sensitivity harness has to be trustworthy before its output is.

An analysis that says "the conclusion is robust" is worth exactly as much as
the machinery producing it. These tests pin the properties that make its
output mean something:

* replay must reproduce a live run's outcomes exactly, or the whole analysis
  is measuring a different system than the one that ran;
* the common-random-numbers scheme must actually hold draws fixed across
  parameter sets, or the sweep measures RNG noise;
* perturbation must stay inside the ranges where each parameter is defined;
* undefined conclusions must be counted as undefined, not as failures.
"""

from __future__ import annotations

import math

import pytest

from recouper.eval.metrics import CaseOutcome
from recouper.eval.replay import CaseTrace, case_uniform, draw_outcomes
from recouper.eval.sensitivity import (
    Robustness,
    influence,
    monte_carlo,
    one_at_a_time,
    seed_stability,
    summarise,
)
from recouper.outcomes.simulate import DEFAULT_PARAMS, PARAM_GROUPS, OutcomeModel


def make_traces(n: int = 120) -> list[CaseTrace]:
    """A batch shaped like a real one: mixed classes, mixed treatment."""
    classes = ["transient", "deferred", "reauth", "instrument_dead", "abandoned_cart"]
    traces = []
    for i in range(n):
        treated = i % 5 != 0
        traces.append(
            CaseTrace(
                case_id=f"case_{i}",
                case_class=classes[i % len(classes)],
                arm="treated" if treated else "control",
                amount_paise=100_00 + (i * 137) % 500_00,
                age_s=86_400 * (i % 30),
                executed=["send_reminder"] if treated and i % 3 == 0 else (
                    ["retry_payment"] if treated and i % 3 == 1 else []
                ),
                contacts_sent=1 if treated and i % 3 == 0 else 0,
                retries_attempted=1 if treated and i % 3 == 1 else 0,
            )
        )
    return traces


# --- the draw is a function of (case, seed), and nothing else ------------


def test_the_same_case_and_seed_always_draw_the_same_uniform():
    assert case_uniform("pay_1", 42) == case_uniform("pay_1", 42)


def test_different_cases_draw_independently():
    values = {case_uniform(f"case_{i}", 7) for i in range(500)}
    assert len(values) == 500, "hash collisions would correlate unrelated cases"


def test_uniforms_are_in_range_and_roughly_uniform():
    values = [case_uniform(f"case_{i}", 1) for i in range(2000)]
    assert all(0.0 <= v < 1.0 for v in values)
    # A badly derived uniform (truncated bits, sign error) shows up here long
    # before it shows up as a subtly wrong recovery rate.
    assert 0.45 < sum(values) / len(values) < 0.55


def test_a_case_draw_does_not_depend_on_batch_position():
    """The property that lets a halted batch be resumed reproducibly."""
    traces = make_traces(40)
    forward = draw_outcomes(traces, seed=3)
    backward = draw_outcomes(list(reversed(traces)), seed=3)

    by_id = {o.case_id: o.recovered for o in forward}
    assert all(by_id[o.case_id] == o.recovered for o in backward)


# --- replay must reproduce the live run ----------------------------------


def test_replay_matches_a_live_run_case_for_case():
    """Replay and the runner must agree, or the analysis studies a fiction."""
    traces = make_traces()
    model = OutcomeModel(seed=99)

    replayed = draw_outcomes(traces, params=DEFAULT_PARAMS, seed=99)
    for trace, outcome in zip(traces, replayed):
        p = model.recovery_probability(
            case_class=trace.case_class,
            age_s=trace.age_s,
            actions=trace.executed,
            contacts_already_sent=trace.contacts_already_sent,
        )
        assert outcome.recovered == (case_uniform(trace.case_id, 99) < p)


def test_control_traces_carry_no_actions_into_the_draw():
    traces = make_traces()
    outcomes = draw_outcomes(traces, seed=1)
    controls = [o for o in outcomes if o.arm == "control"]
    assert controls
    assert all(o.cost_paise == 0 and o.contacts_sent == 0 for o in controls)


# --- common random numbers actually hold the draw fixed ------------------


def test_stronger_actions_can_only_add_recoveries_under_shared_draws():
    """The point of common random numbers, stated as a property.

    Raising every action's odds ratio raises each treated case's recovery
    probability. Because the uniform per case is fixed, a case that recovered
    before must still recover -- the set of recoveries can only grow. If this
    fails, two parameter points are being compared across different random
    samples and every sweep result is noise.
    """
    traces = make_traces()
    weak = draw_outcomes(traces, params=DEFAULT_PARAMS, seed=5)
    strong = draw_outcomes(
        traces, params=DEFAULT_PARAMS.scaled("action_odds_ratio", 2.0), seed=5
    )

    weak_ids = {o.case_id for o in weak if o.recovered}
    strong_ids = {o.case_id for o in strong if o.recovered}
    assert weak_ids <= strong_ids


def test_untreated_cases_are_unaffected_by_action_parameters():
    traces = make_traces()
    base = {o.case_id: o.recovered for o in draw_outcomes(traces, seed=5)}
    scaled = {
        o.case_id: o.recovered
        for o in draw_outcomes(
            traces, params=DEFAULT_PARAMS.scaled("action_odds_ratio", 4.0), seed=5
        )
    }
    for trace in traces:
        if not trace.executed:
            assert base[trace.case_id] == scaled[trace.case_id]


# --- perturbation stays inside the meaningful ranges ---------------------


@pytest.mark.parametrize("factor", [0.01, 0.5, 1.0, 2.0, 50.0])
def test_probabilities_stay_probabilities_however_hard_they_are_scaled(factor):
    p = DEFAULT_PARAMS.scaled("base_self_recovery", factor)
    assert all(0.0 < v < 1.0 for v in p.base_self_recovery.values())


@pytest.mark.parametrize("factor", [0.01, 0.5, 2.0, 50.0])
def test_fatigue_stays_a_discount(factor):
    p = DEFAULT_PARAMS.scaled("contact_fatigue", factor)
    assert all(0.0 <= v <= 1.0 for v in p.contact_fatigue.values())
    assert 0.0 <= p.fatigue_beyond_cap <= 1.0


def test_odds_ratios_may_fall_below_one_but_never_go_negative():
    """A scaled-down ratio means the action HURTS -- a real hypothesis."""
    p = DEFAULT_PARAMS.scaled("action_odds_ratio", 0.1)
    assert p.action_odds_ratio["retry_payment"] < 1.0
    assert all(v > 0 for v in p.action_odds_ratio.values())


def test_a_harmful_action_still_yields_a_valid_probability():
    model = OutcomeModel(seed=1, params=DEFAULT_PARAMS.scaled("action_odds_ratio", 0.01))
    p = model.recovery_probability(
        case_class="deferred", age_s=86_400, actions=["send_reminder", "retry_payment"]
    )
    assert 0.0 < p < 1.0


def test_scaling_an_unknown_group_is_an_error_not_a_silent_no_op():
    """A typo'd group name must not read as 'this parameter does not matter'."""
    with pytest.raises(ValueError):
        DEFAULT_PARAMS.scaled("recovery_vibes", 2.0)


def test_every_declared_group_is_actually_perturbable():
    for group in PARAM_GROUPS:
        assert DEFAULT_PARAMS.scaled(group, 1.5).to_dict() != DEFAULT_PARAMS.to_dict()


# --- the summaries say what they mean ------------------------------------


def test_overstatement_is_undefined_rather_than_absurd_when_lift_is_negative():
    outcomes = [
        CaseOutcome("a", "deferred", "treated", 10_000, True),
        CaseOutcome("b", "deferred", "treated", 10_000, False),
        CaseOutcome("c", "deferred", "control", 10_000, True),
        CaseOutcome("d", "deferred", "control", 10_000, True),
    ]
    s = summarise(outcomes)
    assert s.lift < 0
    assert s.overstatement is None


def test_undefined_conclusions_are_not_counted_as_failures():
    r = Robustness(n_draws=3)
    r.record("c", True)
    r.record("c", None)
    r.record("c", False)
    assert r.defined["c"] == 2
    assert r.rate("c") == pytest.approx(0.5)


def test_a_conclusion_never_defined_reports_no_rate():
    r = Robustness(n_draws=2)
    r.record("c", None)
    r.record("c", None)
    assert r.rate("c") is None


# --- the three passes hold together --------------------------------------


def test_seed_stability_reports_spread_not_a_single_number():
    ss = seed_stability(make_traces(), draws=25, bootstrap_iterations=50)
    assert len(ss.lifts) == 25
    assert ss.sd_lift > 0, "identical lifts across seeds means the draw is stuck"
    assert 0.0 <= ss.positive_fraction <= 1.0


def test_costs_cannot_move_the_recovery_rate():
    """A wiring check: money spent must not change who pays.

    This is the sweep's own control. If cost parameters ever showed up as
    influential, the harness would be leaking them into the outcome model.
    """
    points = one_at_a_time(
        make_traces(), groups=("costs",), factors=(0.5, 1.0, 2.0), seeds=(1, 2, 3)
    )
    assert influence(points)["costs"] == pytest.approx(0.0)


def test_action_strength_is_more_influential_than_cost():
    points = one_at_a_time(
        make_traces(),
        groups=("action_odds_ratio", "costs"),
        factors=(0.5, 1.0, 2.0),
        seeds=(1, 2, 3),
    )
    inf = influence(points)
    assert inf["action_odds_ratio"] > inf["costs"]


def test_monte_carlo_is_deterministic_under_a_fixed_seed():
    traces = make_traces()
    a = monte_carlo(traces, draws=30, seed=11).to_dict()
    b = monte_carlo(traces, draws=30, seed=11).to_dict()
    assert a == b


def test_monte_carlo_reports_a_rate_for_every_conclusion_it_tests():
    rb = monte_carlo(make_traces(), draws=40, seed=3)
    assert set(rb.holds) == {
        "gross_overstates_by_2x_or_more",
        "lift_is_positive",
        "single_batch_lift_exceeds_5pp",
    }
    for name in rb.holds:
        rate = rb.rate(name)
        assert rate is None or (0.0 <= rate <= 1.0 and not math.isnan(rate))
