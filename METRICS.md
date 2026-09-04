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

### One batch first, because it is the honest place to start

| | |
|---|---|
| Treated arm | 277 cases |
| Control arm (untouched) | 63 cases |
| Treated recovery rate | 19.5% |
| **Control self-recovery rate** | **20.6%** |
| **Lift** | **-1.1%** |
| 95% CI (bootstrap, 2000 resamples) | **[-12.5%, +9.2%]** |
| Significant? | **No - the interval contains zero** |

| Money | |
|---|---|
| Gross recovered | Rs.2,80,505 |
| **Incremental recovered** | **-Rs.13,625** |
| 95% CI | [-Rs.1,49,878, +Rs.1,09,687] |
| Overstatement factor | undefined (incremental is negative) |

On seed 42 the agent's treated arm recovered at a *lower* rate than the
control arm. Read literally, this run says the intervention did nothing.

That is kept as the default seed deliberately. It would take one flag to
publish a friendlier batch, and the temptation to do so is exactly what this
document exists to resist.

### But one batch is not a result

```bash
python -m recouper.cli sensitivity --full-seeds 20
```

Running the entire pipeline twenty times - fresh corpus, fresh randomisation,
fresh outcomes each time:

| | |
|---|---|
| Mean lift | **+7.0%** |
| Standard deviation | 4.9pp |
| Range across runs | -0.5% to +17.7% |
| Positive in | **19 / 20 runs** |
| **Significant in** | **8 / 20 runs** |
| Median overstatement | **2.5x** |

Seed 42 is the single worst of the twenty. The effect is almost certainly
real and positive; a batch this size simply cannot demonstrate it more than
about 40% of the time.

**This is the finding.** Not "the agent recovered Rs.X" but: *at this sample
size, a single batch's headline is mostly noise, and any recovery system
reporting one batch as evidence - including this one - is overclaiming.*

A conventional demo would have run once, drawn a good seed, reported
Rs.2.8 lakh gross, and stopped. The distribution above is what that number
actually looks like when you run it twenty times.

## Per-cohort breakdown

The blended number conceals the useful finding. Two cohorts show a
statistically significant effect:

| Cohort | n (T/C) | Treated | Control | Lift | 95% CI | Significant |
|---|---|---|---|---|---|---|
| **Deferred** | 41 / 12 | 43.9% | 16.7% | **+27.2%** | [+0.8%, +51.2%] | **Yes** |
| Transient | 23 / 7 | 56.5% | 42.9% | +13.7% | [-32.3%, +55.3%] | No |
| Overdue invoice | 64 / 6 | 17.2% | 16.7% | +0.5% | [-34.4%, +25.0%] | No |
| Intent-negative | 21 / 3 | 0.0% | 0.0% | 0.0% | [0.0%, 0.0%] | No |
| Abandoned cart | 67 / 23 | 6.0% | 17.4% | -11.4% | [-28.8%, +4.6%] | No |
| Re-auth | 37 / 7 | 16.2% | 28.6% | -12.4% | [-46.3%, +18.9%] | No |
| Instrument-dead | 21 / 4 | 9.5% | 25.0% | -15.5% | [-65.5%, +19.1%] | No |
| Terminal | 1 / 1 | 0.0% | 0.0% | 0.0% | - | No action taken |
| Unclassified | 2 / 0 | - | - | - | - | Arm too small |

### What this actually says

**Where the system earns its keep on this batch:** deferred failures
(+27.2%, the one significant cohort). That is mechanically the most plausible
place for it to work - an insufficient-funds decline retried after payday is
the case where a silent retry does real work and the customer does nothing.

**Where the point estimate is negative:** abandoned carts, re-auth,
instrument-dead. With control arms of 23, 7 and 4, none of these is evidence
of harm; every interval spans zero comfortably. They are reported because a
framework that only surfaces favourable cohorts is not a measurement
framework.

**Compare this table to the cohort table in this document's git history.** On
the previous outcome draw, re-auth and abandoned cart were the two
*significant positive* cohorts, and deferred was not significant. The cohort
ranking is not stable across draws at these arm sizes. Reading a per-cohort
table from one batch and concluding "the system works on re-auth" would have
been wrong then and would be wrong now.

**Intent-negative shows exactly 0.0% in both arms.** That is not a bug: the
class permits at most one soft nudge, and these customers actively cancelled.
The model gives them a 7% base rate decayed by age, and no case in either arm
drew a recovery.

## Cost of intervening

| | |
|---|---|
| Contacts sent | 103 |
| Retries attempted | 27 |
| Total cost | Rs.79.75 |
| **Wasted contacts** | **21** |
| Monetary cost of wasted contacts | Rs.17.25 |

"Wasted contacts" are customers we messaged who would have recovered anyway,
estimated by applying the control-arm self-recovery rate to the contacted
population. **We cannot identify which 21** - that is the fundamental limit
of a randomised design, not a gap in the implementation.

The Rs.17.25 figure understates the real cost. The monetary cost of an
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

## Sensitivity analysis

The disclaimer at the top of this document tells you to distrust the rupee
figures. It does not tell you which findings are properties of the *system*
and which are properties of the numbers in `simulate.py`. This section
answers that.

```bash
python -m recouper.cli sensitivity
```

The agent's behaviour is held fixed throughout. One batch is executed, its
per-case actions recorded as traces, and only the *observation* step is
re-run under different assumptions - so every difference below is
attributable to the parameters, never to the agent quietly doing something
else. Each case keeps one fixed uniform draw across all parameter sets
(common random numbers), so comparisons are not swamped by Monte Carlo noise.

### Which assumptions actually drive the result

Each group scaled 0.5x to 2.0x on its own; the figure is the range of mean
lift that induces.

| Parameter group | Influence on lift |
|---|---|
| `ACTION_ODDS_RATIO` | **10.4pp** |
| `AGE_HALF_LIFE` | 2.5pp |
| `BASE_SELF_RECOVERY` | 2.1pp |
| `CONTACT_FATIGUE` | 1.0pp |
| Unit costs | **0.0pp** |

Two things worth noting. `ACTION_ODDS_RATIO` dominates by roughly 4x, so it
is the number to attack first if you want to argue with the result - not
`BASE_SELF_RECOVERY`, which the original version of this document named as
the most consequential parameter. That was wrong, and the sweep is how it was
found out.

Costs moving the lift by *exactly* zero is the harness's own control: costs
never enter the recovery probability, so any non-zero value there would mean
the analysis was wired wrong rather than that costs matter. A test asserts it
stays zero.

### Does the conclusion survive?

Every group perturbed simultaneously, log-uniform on [0.5x, 2.0x], 400 draws,
each with its own outcome seed:

| Conclusion | Holds in |
|---|---|
| **Gross overstates impact by >=2x** | **86% of draws** (defined in 311) |
| The intervention helps at all | 78% of draws |
| One batch shows a >5pp lift | 39% of draws |

The overstatement finding is robust: across a parameter space where every
assumption may be wrong by up to a factor of two, gross reporting still
overstates impact by at least double in roughly six draws out of seven. It
does not depend on the specific numbers chosen.

The third row is included because it is expected to *fail*. A sensitivity
analysis that only tests conclusions it expects to survive is decoration.

### How much of one batch is luck

Redrawing outcomes 300 times on the same executed batch, parameters
unchanged:

| | |
|---|---|
| Mean lift | +4.1% |
| Standard deviation | 4.8pp |
| 90% of draws | [-4.5%, +11.8%] |
| Positive in | 79% of draws |
| **Its own CI excludes zero in** | **17% of draws** |

A batch of this size would license a claim of significance about one time in
six. Any single-batch headline - including the one at the top of this
document - is mostly a draw.

### What this does not cover

- The randomisation is fixed within a replay, so `seed_stability` measures
  outcome noise only. `--full-seeds` measures the rest, and is the number
  quoted in the headline section.
- Parameters are scaled as groups, not individually. A sweep where
  `retry_payment` moves but `send_reminder` does not would be more precise
  and is not implemented.
- The structural assumptions are untested: that odds ratios compose
  multiplicatively, that age decay is exponential, that contacts fatigue
  independently of channel. Perturbing a parameter cannot tell you the
  functional form is wrong.

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
