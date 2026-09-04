"""Does the conclusion survive the assumptions?

The honest disclaimer in `outcomes/simulate.py` -- "these outcomes are
simulated" -- is necessary but not sufficient. It tells a reviewer to
distrust the rupee figure. It does not tell them which findings are
properties of the *system* and which are properties of the numbers we chose.

This module answers that, in three passes:

seed stability
    Hold the parameters fixed, redraw the outcomes under many random seeds.
    Quantifies how much of a single batch's headline is luck. This is the
    first thing to run against any claim from one batch, and on this corpus
    it is decisive: the batch-to-batch spread is comparable to the effect.

one-at-a-time
    Scale each parameter group across a wide range, holding the rest at
    default. Shows which assumptions actually drive the result and which are
    inert. A finding that flips when `age_half_life` moves 25% is a finding
    about `age_half_life`.

joint Monte Carlo
    Perturb every group at once, drawn log-uniformly, and ask how often each
    qualitative conclusion still holds. This is the number worth reporting:
    "the overstatement finding holds in 97% of the parameter space we
    consider plausible" is a claim that does not rest on any single figure.

Throughout, the agent's behaviour is fixed. Every pass replays the same
`CaseTrace` list (see `replay.py`) rather than re-running the pipeline, so
variation in the output is attributable to the parameters and the draw --
never to the agent having quietly done something different.

Log-uniform rather than uniform sampling
----------------------------------------
The parameters are multiplicative (odds ratios, half-lives, rates), so the
meaningful neighbourhood of a value is a ratio, not a difference. Sampling
uniformly on [0.5, 2.0] would put two-thirds of the mass above 1.0 and
quietly bias the sweep toward stronger assumptions; log-uniform treats "half"
and "double" as equally far from the default, which is what we mean.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..outcomes.simulate import DEFAULT_PARAMS, PARAM_GROUPS, SimParams
from .metrics import CaseOutcome, bootstrap_lift
from .replay import CaseTrace, draw_outcomes


# The range each parameter group is scaled across. Wide on purpose: a
# sensitivity analysis that only wobbles a parameter by 10% is theatre.
# Half to double covers "we were badly wrong but not absurd".
DEFAULT_RANGE: tuple[float, ...] = (0.5, 0.75, 1.0, 1.5, 2.0)

# Seeds reused at every parameter point, so that two points differ by their
# parameters and not by their random draw. See `replay.case_uniform`.
DEFAULT_SEEDS: tuple[int, ...] = tuple(range(40))


@dataclass
class Summary:
    """Point estimates for one (parameters, seed) observation.

    No confidence interval: these are the inputs to a distribution built
    across many observations, and bootstrapping each one would be both
    expensive and a category error -- the spread we care about here is
    across draws, which we have directly.
    """

    treated_rate: float
    control_rate: float
    lift: float
    gross_paise: int
    incremental_paise: int
    cost_paise: int

    @property
    def overstatement(self) -> Optional[float]:
        """How many times larger the gross figure is than the honest one.

        `None` when the incremental figure is zero or negative. A ratio
        against a non-positive denominator is not a large overstatement, it
        is an undefined one, and reporting it as a number would put nonsense
        into the very summary that is supposed to be trustworthy.
        """
        if self.incremental_paise <= 0:
            return None
        return self.gross_paise / self.incremental_paise


def summarise(outcomes: Sequence[CaseOutcome]) -> Summary:
    treated = [o for o in outcomes if o.arm == "treated"]
    control = [o for o in outcomes if o.arm == "control"]
    t_rate = sum(1 for o in treated if o.recovered) / len(treated) if treated else 0.0
    c_rate = sum(1 for o in control if o.recovered) / len(control) if control else 0.0
    lift = t_rate - c_rate

    mean_value = (
        sum(o.amount_paise for o in treated) / len(treated) if treated else 0.0
    )
    return Summary(
        treated_rate=t_rate,
        control_rate=c_rate,
        lift=lift,
        gross_paise=sum(o.amount_paise for o in treated if o.recovered),
        incremental_paise=int(lift * len(treated) * mean_value),
        cost_paise=sum(o.cost_paise for o in outcomes),
    )


def observe(
    traces: Sequence[CaseTrace], params: SimParams, seed: int
) -> Summary:
    return summarise(draw_outcomes(traces, params=params, seed=seed))


# --- pass 1: how much of the headline is luck? ---------------------------


@dataclass
class SeedStability:
    n_draws: int
    lifts: list[float]
    overstatements: list[float]
    significant_fraction: float
    """Share of draws whose own bootstrap CI excludes zero -- i.e. how often
    a single batch would have licensed a claim of effect."""

    @property
    def mean_lift(self) -> float:
        return statistics.fmean(self.lifts)

    @property
    def sd_lift(self) -> float:
        return statistics.pstdev(self.lifts) if len(self.lifts) > 1 else 0.0

    @property
    def positive_fraction(self) -> float:
        return sum(1 for x in self.lifts if x > 0) / len(self.lifts)

    def percentile(self, values: Sequence[float], q: float) -> float:
        if not values:
            return float("nan")
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return ordered[idx]

    def to_dict(self) -> dict:
        return {
            "n_draws": self.n_draws,
            "lift": {
                "mean": round(self.mean_lift, 4),
                "sd": round(self.sd_lift, 4),
                "p05": round(self.percentile(self.lifts, 0.05), 4),
                "p95": round(self.percentile(self.lifts, 0.95), 4),
                "positive_fraction": round(self.positive_fraction, 3),
            },
            "overstatement": {
                "n_defined": len(self.overstatements),
                "median": (
                    round(self.percentile(self.overstatements, 0.5), 2)
                    if self.overstatements else None
                ),
                "p05": (
                    round(self.percentile(self.overstatements, 0.05), 2)
                    if self.overstatements else None
                ),
                "p95": (
                    round(self.percentile(self.overstatements, 0.95), 2)
                    if self.overstatements else None
                ),
            },
            "significant_fraction": round(self.significant_fraction, 3),
        }


@dataclass
class BatchSpread:
    """The same question asked across whole runs, not just redrawn outcomes.

    `seed_stability` holds the corpus and the treatment assignment fixed and
    varies only the outcome draw. That understates the real variation, because
    a fresh run also draws a different corpus and a different randomisation.
    This measures the full thing -- at the cost of actually running the
    pipeline once per seed, which is why it is opt-in.
    """

    lifts: list[float]
    overstatements: list[float]
    significant: int

    @property
    def positive(self) -> int:
        return sum(1 for x in self.lifts if x > 0)

    def to_dict(self) -> dict:
        return {
            "n_runs": len(self.lifts),
            "mean_lift": round(statistics.fmean(self.lifts), 4),
            "sd_lift": round(statistics.pstdev(self.lifts), 4)
            if len(self.lifts) > 1 else 0.0,
            "min_lift": round(min(self.lifts), 4),
            "max_lift": round(max(self.lifts), 4),
            "positive_runs": self.positive,
            "significant_runs": self.significant,
            "median_overstatement": (
                round(statistics.median(self.overstatements), 2)
                if self.overstatements else None
            ),
        }


def seed_stability(
    traces: Sequence[CaseTrace],
    *,
    draws: int = 300,
    params: SimParams = DEFAULT_PARAMS,
    bootstrap_iterations: int = 400,
) -> SeedStability:
    """Redraw outcomes under many seeds, parameters held fixed.

    Note what this does and does not vary. The treatment assignment is part
    of the traces and stays fixed, so this measures outcome noise on one
    randomisation -- not the full sampling distribution. It is therefore a
    *lower* bound on how much a single batch's headline can move, which is
    the conservative direction for a claim about instability.
    """
    lifts: list[float] = []
    overstatements: list[float] = []
    significant = 0

    for seed in range(draws):
        outcomes = draw_outcomes(traces, params=params, seed=seed)
        s = summarise(outcomes)
        lifts.append(s.lift)
        if s.overstatement is not None:
            overstatements.append(s.overstatement)

        ci = bootstrap_lift(
            [o for o in outcomes if o.arm == "treated"],
            [o for o in outcomes if o.arm == "control"],
            iterations=bootstrap_iterations,
            seed=seed,
        )
        if ci.excludes_zero:
            significant += 1

    return SeedStability(
        n_draws=draws,
        lifts=lifts,
        overstatements=overstatements,
        significant_fraction=significant / draws if draws else 0.0,
    )


# --- pass 2: which assumptions actually matter? --------------------------


@dataclass
class SweepPoint:
    group: str
    factor: float
    mean_lift: float
    median_overstatement: Optional[float]
    positive_fraction: float

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "factor": self.factor,
            "mean_lift": round(self.mean_lift, 4),
            "median_overstatement": (
                round(self.median_overstatement, 2)
                if self.median_overstatement is not None else None
            ),
            "positive_fraction": round(self.positive_fraction, 3),
        }


def one_at_a_time(
    traces: Sequence[CaseTrace],
    *,
    groups: Sequence[str] = PARAM_GROUPS,
    factors: Sequence[float] = DEFAULT_RANGE,
    seeds: Sequence[int] = DEFAULT_SEEDS,
    base: SimParams = DEFAULT_PARAMS,
) -> list[SweepPoint]:
    """Scale one group at a time; average over a fixed set of seeds.

    Averaging matters. A single draw at each parameter point would produce a
    sweep curve made mostly of outcome noise -- pass 1 shows that noise is
    the same order as the effect -- and it would be easy to read a jagged
    line as a real non-monotonicity.
    """
    points: list[SweepPoint] = []
    for group in groups:
        for factor in factors:
            params = base if factor == 1.0 else base.scaled(group, factor)
            summaries = [observe(traces, params, seed) for seed in seeds]
            lifts = [s.lift for s in summaries]
            overs = [
                s.overstatement for s in summaries if s.overstatement is not None
            ]
            points.append(
                SweepPoint(
                    group=group,
                    factor=factor,
                    mean_lift=statistics.fmean(lifts),
                    median_overstatement=statistics.median(overs) if overs else None,
                    positive_fraction=sum(1 for x in lifts if x > 0) / len(lifts),
                )
            )
    return points


def influence(points: Sequence[SweepPoint]) -> dict[str, float]:
    """Range of mean lift induced by each group across the sweep.

    A crude but honest importance measure: how far the headline moves when
    this assumption alone is wrong by up to a factor of two.
    """
    out: dict[str, float] = {}
    for group in {p.group for p in points}:
        lifts = [p.mean_lift for p in points if p.group == group]
        out[group] = max(lifts) - min(lifts)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --- pass 3: does the conclusion hold across the whole space? ------------


@dataclass
class Robustness:
    """How often each qualitative conclusion survives a joint perturbation."""

    n_draws: int
    holds: dict[str, int] = field(default_factory=dict)
    defined: dict[str, int] = field(default_factory=dict)

    def record(self, name: str, verdict: Optional[bool]) -> None:
        """`None` means the conclusion is not even defined for this draw.

        Counted separately rather than as a failure. "Undefined in 12% of
        draws" and "false in 12% of draws" are different facts about a
        finding, and collapsing them would overstate its fragility.
        """
        self.defined.setdefault(name, 0)
        self.holds.setdefault(name, 0)
        if verdict is None:
            return
        self.defined[name] += 1
        if verdict:
            self.holds[name] += 1

    def rate(self, name: str) -> Optional[float]:
        n = self.defined.get(name, 0)
        return self.holds[name] / n if n else None

    def to_dict(self) -> dict:
        return {
            "n_draws": self.n_draws,
            "conclusions": {
                name: {
                    "defined_in": self.defined.get(name, 0),
                    "held_in": self.holds.get(name, 0),
                    "rate": (
                        round(self.rate(name), 3)
                        if self.rate(name) is not None else None
                    ),
                }
                for name in sorted(self.holds)
            },
        }


def _log_uniform(rng: random.Random, low: float, high: float) -> float:
    return math.exp(rng.uniform(math.log(low), math.log(high)))


def monte_carlo(
    traces: Sequence[CaseTrace],
    *,
    draws: int = 400,
    low: float = 0.5,
    high: float = 2.0,
    seed: int = 7,
    base: SimParams = DEFAULT_PARAMS,
) -> Robustness:
    """Perturb every parameter group at once and test the conclusions.

    Each draw also gets its own outcome seed, so the reported rates fold in
    both sources of uncertainty -- wrong assumptions and unlucky draws --
    rather than reporting parameter uncertainty against a single fixed
    sample and calling the result robust.
    """
    rng = random.Random(seed)
    result = Robustness(n_draws=draws)

    for i in range(draws):
        params = base
        for group in PARAM_GROUPS:
            params = params.scaled(group, _log_uniform(rng, low, high))
        s = observe(traces, params, seed=rng.randrange(1 << 30))

        # C1: the point of the whole project -- reporting gross materially
        # overstates what the agent achieved.
        result.record(
            "gross_overstates_by_2x_or_more",
            None if s.overstatement is None else s.overstatement >= 2.0,
        )
        # C2: the intervention helps at all.
        result.record("lift_is_positive", s.lift > 0)
        # C3: one batch of this size can tell you C2. Recorded because we
        # expect it to FAIL most of the time; a sensitivity analysis that
        # only tests conclusions it expects to pass is decoration.
        result.record("single_batch_lift_exceeds_5pp", s.lift > 0.05)

    return result
