"""Separating what the agent DID from what the simulator ASSUMED.

The agent's behaviour -- which cases it worked, what the planner chose, what
the policy engine allowed -- depends on the corpus, the classifier, the plan
tables and the rules. It does **not** depend on the outcome model. Nothing in
`policy/`, `agent/` or `detect/` ever reads a recovery probability.

That independence is what makes a sensitivity analysis tractable and, more
importantly, what makes it *mean* something. A run is split in two:

    1. execution  -- work the batch, record a `CaseTrace` per case
    2. observation -- draw outcomes from those traces under some `SimParams`

Step 2 can then be repeated thousands of times against different parameters
while step 1 stays fixed, so any variation in the reported metrics is
attributable to the parameters alone. Re-running the whole pipeline per draw
would instead vary the agent's behaviour too and confound the two.

Common random numbers
---------------------
Each case is assigned one uniform draw, fixed by the run seed and the case
id, and reused across every parameter set. A case is recovered when that
uniform falls below the probability the parameters imply.

Without this, comparing two parameter sets compares two different random
samples, and small genuine differences vanish into Monte Carlo noise --
you would need orders of magnitude more draws to see the same signal. With
it, a case that recovers under weak assumptions also recovers under stronger
ones, so the sweep isolates the parameter effect rather than the RNG.

Deriving the uniform from the case id rather than from position in the list
matters too: it keeps a case's draw stable even if the batch reorders,
halts early, or is resumed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..outcomes.simulate import DEFAULT_PARAMS, OutcomeModel, SimParams
from .metrics import CaseOutcome


@dataclass
class CaseTrace:
    """Everything the outcome model needs, and nothing it does not.

    Deliberately holds no `recovered` field. A trace records what we did to a
    case, which is fact; whether the customer paid is a draw, which is
    assumption, and keeping the two in separate types stops them being
    conflated in the reports downstream.
    """

    case_id: str
    case_class: str
    arm: str  # "treated" | "control"
    amount_paise: int
    age_s: int
    executed: list[str] = field(default_factory=list)
    contacts_already_sent: int = 0
    contacts_sent: int = 0
    retries_attempted: int = 0
    llm_used: bool = False
    escalated: bool = False
    excepted: bool = False

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "case_class": self.case_class,
            "arm": self.arm,
            "amount_paise": self.amount_paise,
            "age_s": self.age_s,
            "executed": list(self.executed),
            "contacts_already_sent": self.contacts_already_sent,
            "contacts_sent": self.contacts_sent,
            "retries_attempted": self.retries_attempted,
            "llm_used": self.llm_used,
            "escalated": self.escalated,
            "excepted": self.excepted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CaseTrace":
        return cls(
            case_id=data["case_id"],
            case_class=data["case_class"],
            arm=data["arm"],
            amount_paise=int(data["amount_paise"]),
            age_s=int(data["age_s"]),
            executed=list(data.get("executed", [])),
            contacts_already_sent=int(data.get("contacts_already_sent", 0)),
            contacts_sent=int(data.get("contacts_sent", 0)),
            retries_attempted=int(data.get("retries_attempted", 0)),
            llm_used=bool(data.get("llm_used", False)),
            escalated=bool(data.get("escalated", False)),
            excepted=bool(data.get("excepted", False)),
        )


def case_uniform(case_id: str, seed: int) -> float:
    """The fixed uniform draw for one case under one run seed.

    A hash rather than a `random.Random` stream so that it depends only on
    (case_id, seed) -- not on how many cases were drawn before it. That is
    what lets a resumed or reordered batch reproduce the same outcomes.
    """
    digest = hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).digest()
    # 53 bits: the most a float64 can hold without rounding, so distinct
    # digests stay distinct after the division.
    value = int.from_bytes(digest[:7], "big") >> 3
    return value / float(1 << 53)


def draw_outcomes(
    traces: Sequence[CaseTrace],
    *,
    params: SimParams = DEFAULT_PARAMS,
    seed: int = 2026,
    model: Optional[OutcomeModel] = None,
) -> list[CaseOutcome]:
    """Observe a set of traces under one parameter set."""
    model = model or OutcomeModel(seed=seed, params=params)

    outcomes: list[CaseOutcome] = []
    for trace in traces:
        p = model.recovery_probability(
            case_class=trace.case_class,
            age_s=trace.age_s,
            actions=trace.executed,
            contacts_already_sent=trace.contacts_already_sent,
        )
        outcomes.append(
            CaseOutcome(
                case_id=trace.case_id,
                case_class=trace.case_class,
                arm=trace.arm,
                amount_paise=trace.amount_paise,
                recovered=case_uniform(trace.case_id, seed) < p,
                contacts_sent=trace.contacts_sent,
                retries_attempted=trace.retries_attempted,
                cost_paise=model.action_cost_paise(
                    trace.executed, llm_used=trace.llm_used
                ),
                escalated=trace.escalated,
                excepted=trace.excepted,
            )
        )
    return outcomes
