# Results

Reproduce everything here with:

```bash
python -m recouper.cli run --no-llm --json runs/result_seed42.json
```

Seed 42, corpus of 955 synthetic records, batch executed at 2026-08-30 11:00 IST.

---

## The caveat that governs every number below

**All recovery outcomes are simulated.** Test-mode APIs cannot make a
synthetic customer decide to pay. The final "did they pay?" step is drawn
from the explicit model in `recouper/outcomes/simulate.py`; print it with
`python -m recouper.cli params`.

Every rupee figure here is therefore a property of those parameters. The
contribution is the measurement framework and the bounded execution — not
the absolute amounts.

**The parameters were fixed before any recovery number was computed and have
not been adjusted since.** Tuning a simulator until the agent looks good
would make the exercise circular. Git history is the evidence.

---

## Case extraction

| | |
|---|---|
| Corpus records | 955 |
| Cases extracted | **340** |
| — failed payments | 180 |
| — abandoned checkouts | 90 |
| — overdue invoices | 70 |
| Actionable | 336 |
| Unclassified → exception list | 2 |
| Terminal (fraud) → hard stop | 2 |
| **Orders deduplicated** | **180** |

That deduplication line matters. An order carrying a failed payment satisfies
both "failed payment" and "abandoned checkout" — status `attempted`,
`amount_paid = 0`. Counting both would have inflated the denominator from 340
to 520, silently deflating every recovery rate by ~35%. Nothing would have
crashed. See `JOURNAL.md`, Day 1.

---

## Headline result

| | |
|---|---|
| Treated arm | 277 cases |
| Control arm (untouched) | 63 cases |
| Treated recovery rate | 17.3% |
| **Control self-recovery rate** | **12.7%** |
| **Lift** | **+4.6%** |
| 95% CI (bootstrap, 2000 resamples) | **[−4.8%, +13.4%]** |
| Significant? | **No — the interval contains zero** |

| Money | |
|---|---|
| Gross recovered | ₹3,18,352 |
| **Incremental recovered** | **₹55,323** |
| 95% CI | [−₹56,760, +₹1,59,669] |
| **Overstatement factor** | **5.8×** |

### Reading this honestly

The headline conclusion is **"this batch does not demonstrate a significant
aggregate effect."**

A conventional recovery demo would report **₹3.18 lakh recovered** here. That
figure is real in the sense that the money arrived — and misleading, because
12.7% of the control arm recovered with no intervention whatsoever. The
system's actual contribution is at most ₹55,323, and the confidence interval
does not exclude zero.

The gap between those two numbers — **5.8×** — is the entire argument for
running a control arm.

The interval is wide because the control arm is small (63 cases). That is a
real limitation and it is reported rather than engineered away: enlarging the
corpus until the result crossed into significance would have been trivial and
dishonest.

---

## Per-cohort breakdown

The blended number conceals the useful finding. Two cohorts show a
statistically significant effect:

| Cohort | n (T/C) | Treated | Control | Lift | 95% CI | Significant |
|---|---|---|---|---|---|---|
| **Re-auth** | 37 / 7 | 21.6% | 0.0% | **+21.6%** | [+8.1%, +35.1%] | **Yes** |
| **Abandoned cart** | 67 / 23 | 10.4% | 0.0% | **+10.4%** | [+4.5%, +17.9%] | **Yes** |
| Deferred | 41 / 12 | 24.4% | 16.7% | +7.7% | [−18.7%, +31.7%] | No |
| Transient | 23 / 7 | 47.8% | 42.9% | +5.0% | [−36.6%, +46.6%] | No |
| Intent-negative | 21 / 3 | 4.8% | 0.0% | +4.8% | [0.0%, +14.3%] | No |
| Instrument-dead | 21 / 4 | 14.3% | 25.0% | −10.7% | [−60.7%, +23.8%] | No |
| Overdue invoice | 64 / 6 | 12.5% | 33.3% | −20.8% | [−60.4%, +15.6%] | No |
| Terminal | 1 / 1 | 0.0% | 0.0% | 0.0% | — | No action taken |
| Unclassified | 2 / 0 | — | — | — | — | Arm too small |

### What this actually says

**Where the system earns its keep:** re-auth failures (+21.6%) and abandoned
carts (+10.4%). Both make sense mechanically — in each, the customer had
clear intent and hit a friction point, and a payment link removes exactly
that friction. Neither cohort self-recovers at all in the control arm, so
essentially all recovery here is attributable.

**Where it does not:** transient failures show +5.0% with an interval spanning
[−36.6%, +46.6%] — completely uninformative. And note the control rate is
42.9%: these customers overwhelmingly recover on their own. Intervening here
is close to pure waste, which is precisely the sort of spending a gross-only
report would have justified.

**Where it may be actively harmful:** overdue invoices show a *negative* point
estimate (−20.8%). The control arm is 6 cases, so this is not evidence of
harm — the interval is far too wide to conclude anything. But it is exactly
the signal that should trigger a properly powered follow-up rather than being
clamped to zero and forgotten. The framework reports negative lift when it
sees it.

**Honest note on the two significant results:** control arms of 7 and 23 are
small. A control rate of exactly 0.0% in both is partly a small-sample
artefact. The direction is credible and mechanically plausible; the magnitude
should be treated as provisional.

---

## Cost of intervening

| | |
|---|---|
| Contacts sent | 103 |
| Retries attempted | 27 |
| Total cost | ₹79.75 |
| Cost per ₹1 recovered | ₹0.0014 |
| **Wasted contacts** | **13** |
| Monetary cost of wasted contacts | ₹15.25 |

"Wasted contacts" are customers we messaged who would have recovered anyway,
estimated by applying the control-arm self-recovery rate to the contacted
population. **We cannot identify which 13** — that is the fundamental limit
of a randomised design, not a gap in the implementation.

The ₹15.25 figure understates the real cost. The monetary cost of an
unnecessary email is trivial; the goodwill cost is not, and nothing here
captures it. That asymmetry is why the contact caps are conservative.

---

## The bounds actually bound

| Rule | Actions blocked |
|---|---|
| `contact_cooldown` | 153 |
| `do_not_contact` | 8 |
| `amount_ceiling` | 2 (escalated to a human) |
| Escalated to human | 4 |
| Exceptions | 3 |
| Dead letters | 0 (clean run) |

`contact_cooldown` dominating is expected and correct: the batch processes
all cases at one timestamp, so a customer with several debts is legitimately
blocked after the first contact. This is the multi-debt pile-on rule doing
its job — without it, those 153 actions would have been additional messages
to people already contacted that day.

A run where no rule ever fires would mean the bounds are untested claims.

---

## Failure handling

```bash
python -m recouper.cli run --no-llm --faults
```

```
--- BATCH HALTED ---
  10 failures (20%) over the last 50 actions crossed the 20% threshold
  10 cases parked in the DLQ with context.
  Resume is a manual operation -- the breaker does not self-heal.

Audit chain       chain intact across 192 records
```

Note that a *moderate* fault rate does **not** halt the batch. At ~45%
per-call failures, three transport retries mean only ~9% of actions exhaust —
below the threshold, so the batch correctly rides it out. The breaker exists
for outages retries cannot absorb, not for degradation they can.

---

## Audit trail

```bash
python -m recouper.cli verify runs/audit_seed42.jsonl
# chain intact across 576 records
```

576 records for 340 cases — because denials, escalations and plans are logged
alongside executed actions. Roughly half the ledger is things the system
decided *not* to do.

---

## Sensitivities worth checking

Ordered by how much they'd move the result:

1. **`BASE_SELF_RECOVERY`** — directly sets the control-arm rate, so it
   directly sets the gap between gross and incremental. The single most
   consequential set of numbers in the project.
2. **21-day age half-life** — a guess. Governs how fast a debt becomes
   unrecoverable, so it shifts which cohorts look worth working.
3. **`ACTION_ODDS_RATIO`** — sets treated-arm uplift. Modelled as odds ratios
   rather than additive bumps, so effects compose correctly and stay in range.
4. **Control fraction (20%)** — the precision/cost trade-off. A larger control
   arm would tighten every interval here at the cost of recovering less money
   in the run itself.

Change any of these in `recouper/outcomes/simulate.py` and re-run; the
framework is the deliverable, and it will report whatever the parameters
imply — including that the agent achieved nothing.

---

## What I'd do with real data

- Replace the outcome model with observed recovery, which removes the caveat
  governing this entire document.
- Run a properly powered test on overdue invoices specifically. The negative
  point estimate is uninformative at n=6 control but is the most interesting
  thing in the table.
- Measure contact fatigue empirically instead of assuming the decay curve.
- Add a long-horizon check on whether recovered customers churn later — a
  recovery that costs you the relationship is not a recovery, and nothing
  here would currently detect that.
