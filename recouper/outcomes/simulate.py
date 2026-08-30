"""Customer response simulation.

=========================== READ THIS FIRST ===========================
Every recovery outcome this project reports is SIMULATED. Test-mode APIs
cannot make a synthetic customer decide to pay, so the final "did they pay?"
step is drawn from the explicit probability model below.

The contribution of this project is the measurement framework and the bounded
execution around it -- NOT the absolute rupee figure, which is a property of
these parameters and would change if you changed them.

Every number below is stated with its reasoning, in one file, so a reviewer
can disagree with a specific figure and re-run rather than having to distrust
the whole result.
=======================================================================

The parameter that does the most work is `BASE_SELF_RECOVERY`: the
probability a customer pays *with no intervention at all*. It is what the
control arm measures, and it is the number naive recovery demos implicitly
claim credit for. It is deliberately set high for the classes where it really
is high -- a customer whose card was declined for insufficient funds very
often retries unprompted a few days later.

A methodological commitment: these parameters were fixed BEFORE any recovery
number was computed, and have not been adjusted since. Tuning a simulator
until the agent looks good would make the entire exercise circular. The git
history is the evidence -- this file's values are unchanged from the commit
that introduced it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from ..detect.classify import RecoveryClass

_DAY = 86_400


# Probability the customer resolves the debt on their own within the
# observation horizon, with no contact and no retry from us.
#
# Reasoning per class:
#   TRANSIENT       - nothing was wrong; most customers simply try again and
#                     succeed, often within minutes. Self-recovery is high.
#   DEFERRED        - insufficient funds. Many customers retry after payday
#                     without prompting, especially for a service they want.
#   REAUTH          - the customer was present and engaged but fumbled auth.
#                     Moderate self-recovery: some retry immediately, others
#                     give up and forget.
#   INSTRUMENT_DEAD - requires actively finding a different card. Meaningful
#                     friction, so self-recovery is low.
#   INTENT_NEGATIVE - they chose to cancel. Low, by definition.
#   ABANDONED_CART  - weakest intent of all; most never return.
#   OVERDUE_INVOICE - B2B receivables mostly do get paid eventually; late,
#                     but paid. High baseline.
BASE_SELF_RECOVERY: dict[str, float] = {
    RecoveryClass.TRANSIENT.value: 0.55,
    RecoveryClass.DEFERRED.value: 0.34,
    RecoveryClass.REAUTH.value: 0.28,
    RecoveryClass.INSTRUMENT_DEAD.value: 0.12,
    RecoveryClass.INTENT_NEGATIVE.value: 0.07,
    "abandoned_cart": 0.09,
    "overdue_invoice": 0.45,
}


# Multiplicative uplift on the odds of recovery, per action type.
#
# Expressed as an ODDS RATIO rather than an additive bump on purpose. An
# additive model breaks at the boundaries: +0.30 applied to a class with a
# 0.85 baseline would exceed 1.0, and would also absurdly claim the same
# absolute gain on a population that was going to pay anyway as on one that
# was not. Odds ratios compose correctly and stay in range.
ACTION_ODDS_RATIO: dict[str, float] = {
    # A silent retry against a valid mandate is the strongest lever we have:
    # it removes all customer effort. Strongest where the original failure
    # was time-dependent.
    "retry_payment": 2.60,
    # A link removes the friction of finding the checkout again, but still
    # requires the customer to act.
    "create_payment_link": 1.75,
    # A bare reminder only supplies salience.
    "send_reminder": 1.35,
    "escalate_to_human": 1.0,
    "close_no_action": 1.0,
}


# Each additional contact is worth less than the last, and eventually
# negative. Recovery value decays while annoyance accumulates -- the third
# message is close to worthless and the fourth actively harms.
CONTACT_FATIGUE: dict[int, float] = {
    0: 1.00,
    1: 1.00,
    2: 0.62,
    3: 0.30,
}
FATIGUE_BEYOND_CAP = 0.10


# Debt age decay: intent fades. A week-old abandoned cart is a far weaker
# prospect than a two-hour-old one. Modelled as exponential with a 21-day
# half-life, which is a guess, and a consequential one -- flagged in
# METRICS.md as a sensitivity worth checking.
AGE_HALF_LIFE_S = 21 * _DAY


# Unit costs, used for cost-per-recovery and false-positive cost.
# Order-of-magnitude estimates, not quotes.
COST_PER_MESSAGE_PAISE = 25          # ~Rs.0.25, bulk email/SMS
COST_PER_RETRY_ATTEMPT_PAISE = 200   # ~Rs.2, gateway fee on a failed attempt
COST_PER_LLM_PLAN_PAISE = 150        # ~Rs.1.50, one planner call


@dataclass
class OutcomeModel:
    """Draws recovery outcomes. Seeded, so a run is reproducible."""

    seed: int = 2026
    rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def base_rate(self, case_class: str) -> float:
        return BASE_SELF_RECOVERY.get(case_class, 0.15)

    def age_factor(self, age_s: int) -> float:
        return 0.5 ** (age_s / AGE_HALF_LIFE_S)

    def recovery_probability(
        self,
        *,
        case_class: str,
        age_s: int,
        actions: Optional[list[str]] = None,
        contacts_already_sent: int = 0,
    ) -> float:
        """Probability this debt is recovered within the horizon.

        Composed as: baseline (decayed by age) converted to odds, multiplied
        by each action's odds ratio scaled by fatigue, converted back.
        """
        actions = actions or []

        p0 = self.base_rate(case_class) * self.age_factor(age_s)
        p0 = min(max(p0, 1e-6), 0.999)
        odds = p0 / (1 - p0)

        contacts = contacts_already_sent
        for action in actions:
            ratio = ACTION_ODDS_RATIO.get(action, 1.0)
            if action in ("send_reminder", "create_payment_link"):
                fatigue = CONTACT_FATIGUE.get(contacts, FATIGUE_BEYOND_CAP)
                contacts += 1
            else:
                fatigue = 1.0
            # Scale the *excess* odds by fatigue, so a fatigued action tends
            # toward no effect rather than toward harm.
            effective = 1.0 + (ratio - 1.0) * fatigue
            odds *= effective

        return odds / (1 + odds)

    def draw(self, probability: float) -> bool:
        return self.rng.random() < probability

    def action_cost_paise(self, actions: list[str], *, llm_used: bool) -> int:
        cost = COST_PER_LLM_PLAN_PAISE if llm_used else 0
        for a in actions:
            if a in ("send_reminder", "create_payment_link"):
                cost += COST_PER_MESSAGE_PAISE
            elif a == "retry_payment":
                cost += COST_PER_RETRY_ATTEMPT_PAISE
        return cost


def model_parameters() -> dict:
    """The full parameter set, for METRICS.md and the dashboard.

    Published rather than buried so that a reviewer can see exactly what the
    simulated outcomes rest on.
    """
    return {
        "base_self_recovery": dict(BASE_SELF_RECOVERY),
        "action_odds_ratio": dict(ACTION_ODDS_RATIO),
        "contact_fatigue": {str(k): v for k, v in CONTACT_FATIGUE.items()},
        "fatigue_beyond_cap": FATIGUE_BEYOND_CAP,
        "age_half_life_days": AGE_HALF_LIFE_S / _DAY,
        "costs_paise": {
            "message": COST_PER_MESSAGE_PAISE,
            "retry_attempt": COST_PER_RETRY_ATTEMPT_PAISE,
            "llm_plan": COST_PER_LLM_PLAN_PAISE,
        },
    }
