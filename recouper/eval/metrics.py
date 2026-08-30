"""Honest measurement.

The central claim of this project lives in this file.

A recovery system that reports "we chased 200 failed payments and Rs.4 lakh
came in" is reporting a number that is not true. A large share of those
customers would have paid anyway; the system sent an email first and took
credit for the outcome. The gross figure measures the population's tendency
to self-recover far more than it measures the agent.

So every batch randomises cases into a treated arm and an untouched control
arm, and the headline number is the difference:

    incremental recovery = treated_rate - control_rate

with a bootstrap confidence interval, because a difference between two noisy
proportions on a few hundred cases is itself noisy, and reporting it as a
point estimate would be the same overclaiming in a new outfit.

If the interval contains zero, we say so. A system that reports "no
significant lift on this cohort" is more credible, not less -- and vastly
more useful, because it tells you where to stop spending.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass
class CaseOutcome:
    """One case's result at the end of the observation horizon."""

    case_id: str
    case_class: str
    arm: str  # "treated" | "control"
    amount_paise: int
    recovered: bool
    contacts_sent: int = 0
    retries_attempted: int = 0
    cost_paise: int = 0
    escalated: bool = False
    excepted: bool = False


@dataclass
class ProportionCI:
    point: float
    low: float
    high: float

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0 or self.high < 0

    def to_dict(self) -> dict:
        return {
            "point": round(self.point, 4),
            "ci_low": round(self.low, 4),
            "ci_high": round(self.high, 4),
            "significant": self.excludes_zero,
        }


def _rate(outcomes: Sequence[CaseOutcome]) -> float:
    if not outcomes:
        return 0.0
    return sum(1 for o in outcomes if o.recovered) / len(outcomes)


def bootstrap_lift(
    treated: Sequence[CaseOutcome],
    control: Sequence[CaseOutcome],
    *,
    iterations: int = 2000,
    alpha: float = 0.05,
    seed: int = 99,
) -> ProportionCI:
    """Percentile bootstrap CI on the difference in recovery rates.

    Bootstrap rather than a normal approximation because the control arm is
    small by construction (20% of the batch) and recovery rates for some
    classes sit near 0, where the normal approximation's symmetric interval
    can extend below zero into impossible territory. Resampling makes no
    distributional assumption and cannot produce an interval outside the
    achievable range.
    """
    point = _rate(treated) - _rate(control)
    if not treated or not control:
        return ProportionCI(point, float("nan"), float("nan"))

    rng = random.Random(seed)
    t_flags = [o.recovered for o in treated]
    c_flags = [o.recovered for o in control]
    nt, nc = len(t_flags), len(c_flags)

    diffs = []
    for _ in range(iterations):
        t = sum(t_flags[rng.randrange(nt)] for _ in range(nt)) / nt
        c = sum(c_flags[rng.randrange(nc)] for _ in range(nc)) / nc
        diffs.append(t - c)

    diffs.sort()
    lo = diffs[int((alpha / 2) * iterations)]
    hi = diffs[min(int((1 - alpha / 2) * iterations), iterations - 1)]
    return ProportionCI(point, lo, hi)


@dataclass
class BatchMetrics:
    """The full, honest result of one batch."""

    n_treated: int
    n_control: int

    treated_rate: float
    control_rate: float
    lift: ProportionCI

    gross_recovered_paise: int
    incremental_recovered_paise: int
    incremental_recovered_ci_paise: Optional[tuple[int, int]]

    total_cost_paise: int
    contacts_sent: int
    retries_attempted: int

    wasted_contacts: int
    wasted_contact_cost_paise: int

    escalated: int
    excepted: int

    per_class: dict = field(default_factory=dict)

    # --- derived -----------------------------------------------------------

    @property
    def self_recovery_rate(self) -> float:
        """What the control arm did unaided.

        The number a gross-reporting system silently claims as its own.
        """
        return self.control_rate

    @property
    def overstatement_factor(self) -> Optional[float]:
        """How many times larger the gross claim is than the honest one."""
        if self.incremental_recovered_paise <= 0:
            return None
        return self.gross_recovered_paise / self.incremental_recovered_paise

    @property
    def cost_per_rupee_recovered(self) -> Optional[float]:
        if self.incremental_recovered_paise <= 0:
            return None
        return self.total_cost_paise / self.incremental_recovered_paise

    @property
    def contacts_per_recovery(self) -> Optional[float]:
        net = self.incremental_recovered_paise
        if net <= 0 or self.n_treated == 0:
            return None
        incremental_cases = self.lift.point * self.n_treated
        if incremental_cases <= 0:
            return None
        return self.contacts_sent / incremental_cases

    def to_dict(self) -> dict:
        return {
            "arms": {"treated": self.n_treated, "control": self.n_control},
            "recovery_rates": {
                "treated": round(self.treated_rate, 4),
                "control_self_recovery": round(self.control_rate, 4),
                "lift": self.lift.to_dict(),
            },
            "money_paise": {
                "gross_recovered": self.gross_recovered_paise,
                "gross_recovered_note": (
                    "INFLATED -- includes customers who would have paid with "
                    "no intervention. Not a claim of impact."
                ),
                "incremental_recovered": self.incremental_recovered_paise,
                "incremental_ci": (
                    list(self.incremental_recovered_ci_paise)
                    if self.incremental_recovered_ci_paise else None
                ),
                "overstatement_factor": (
                    round(self.overstatement_factor, 2)
                    if self.overstatement_factor else None
                ),
            },
            "cost_paise": {
                "total": self.total_cost_paise,
                "per_rupee_recovered": (
                    round(self.cost_per_rupee_recovered, 4)
                    if self.cost_per_rupee_recovered else None
                ),
            },
            "false_positives": {
                "wasted_contacts": self.wasted_contacts,
                "wasted_contact_cost_paise": self.wasted_contact_cost_paise,
                "note": (
                    "Contacts sent to customers who recovered anyway. The "
                    "monetary cost is small; the goodwill cost is real and "
                    "not captured by this number."
                ),
            },
            "activity": {
                "contacts_sent": self.contacts_sent,
                "retries_attempted": self.retries_attempted,
                "escalated_to_human": self.escalated,
                "exceptions": self.excepted,
            },
            "per_class": self.per_class,
        }


def compute(
    outcomes: Sequence[CaseOutcome],
    *,
    bootstrap_iterations: int = 2000,
    seed: int = 99,
) -> BatchMetrics:
    treated = [o for o in outcomes if o.arm == "treated"]
    control = [o for o in outcomes if o.arm == "control"]

    t_rate, c_rate = _rate(treated), _rate(control)
    lift = bootstrap_lift(
        treated, control, iterations=bootstrap_iterations, seed=seed
    )

    gross = sum(o.amount_paise for o in treated if o.recovered)

    # Incremental rupees: apply the lift to the treated cohort's value.
    #
    # Uses the treated arm's MEAN case value rather than the value of the
    # recovered cases, because the lift is a rate over the whole cohort. The
    # recovered subset skews toward whatever amounts happen to correlate with
    # recovery, and multiplying a cohort-level rate by a self-selected
    # subset's mean would mix two different populations.
    mean_value = (
        sum(o.amount_paise for o in treated) / len(treated) if treated else 0
    )
    incremental = int(lift.point * len(treated) * mean_value)

    # With no control arm there is no lift to bound, and bootstrap_lift
    # returns NaN bounds to say so. Propagate that as "no claim" rather than
    # coercing it to a number -- a zero here would read as a measured
    # finding of no effect, which is a different and much stronger statement
    # than "we did not measure this".
    if math.isnan(lift.low) or math.isnan(lift.high):
        incremental_ci = None
    else:
        incremental_ci = (
            int(lift.low * len(treated) * mean_value),
            int(lift.high * len(treated) * mean_value),
        )

    # False positives: treated customers we contacted who ALSO would have
    # recovered unaided. We cannot identify these individually -- that is the
    # fundamental limit of a randomised design -- so we estimate the count as
    # the control-arm self-recovery rate applied to the contacted population.
    contacted = [o for o in treated if o.contacts_sent > 0]
    wasted = int(round(c_rate * len(contacted)))
    wasted_cost = sum(
        o.cost_paise for o in sorted(contacted, key=lambda o: -o.cost_paise)[:wasted]
    )

    return BatchMetrics(
        n_treated=len(treated),
        n_control=len(control),
        treated_rate=t_rate,
        control_rate=c_rate,
        lift=lift,
        gross_recovered_paise=gross,
        incremental_recovered_paise=incremental,
        incremental_recovered_ci_paise=incremental_ci,
        total_cost_paise=sum(o.cost_paise for o in outcomes),
        contacts_sent=sum(o.contacts_sent for o in outcomes),
        retries_attempted=sum(o.retries_attempted for o in outcomes),
        wasted_contacts=wasted,
        wasted_contact_cost_paise=wasted_cost,
        escalated=sum(1 for o in outcomes if o.escalated),
        excepted=sum(1 for o in outcomes if o.excepted),
        per_class=_per_class(outcomes, bootstrap_iterations, seed),
    )


def _per_class(
    outcomes: Sequence[CaseOutcome], iterations: int, seed: int
) -> dict:
    """Break the lift down by case class.

    Worth reporting separately because a single blended number hides the
    thing an operator most needs to know: which cohorts the intervention
    actually helps, and which it is merely spending money on.
    """
    classes = sorted({o.case_class for o in outcomes})
    out = {}
    for cls in classes:
        t = [o for o in outcomes if o.case_class == cls and o.arm == "treated"]
        c = [o for o in outcomes if o.case_class == cls and o.arm == "control"]
        if not t or not c:
            out[cls] = {
                "n_treated": len(t),
                "n_control": len(c),
                "note": "arm too small to estimate a lift",
            }
            continue
        ci = bootstrap_lift(t, c, iterations=min(iterations, 1000), seed=seed)
        out[cls] = {
            "n_treated": len(t),
            "n_control": len(c),
            "treated_rate": round(_rate(t), 4),
            "control_rate": round(_rate(c), 4),
            "lift": ci.to_dict(),
        }
    return out
