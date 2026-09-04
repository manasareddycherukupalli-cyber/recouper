# Recouper

**A bounded, auditable revenue-recovery agent.**
Razorpay AI Buildathon — Track 3: AI Revenue Recovery

| | |
|---|---|
| **Walkthrough** | **https://razorpay-rust-six.vercel.app** — static, always instant |
| **Live dashboard** | _(deploying)_ — free tier; if it has idled, the first request takes ~30s to wake |
| **Run it locally** | `pip install -r requirements.txt` then `python -m uvicorn recouper.webapp:app` |

If a hosted link is slow or unreachable, nothing is lost — the entire system
runs offline from this repository with no API key and no external service.
The two commands above are the whole setup.

---

## What it does

Finds money that was lost rather than refused — failed payments, abandoned
checkouts, overdue invoices — decides what can honestly be done about each
one, and does it under hard constraints it cannot talk its way past.

Then it measures its own impact in a way that survives scrutiny.

```bash
pip install -r requirements.txt
python -m recouper.cli run --no-llm
```

That is the whole setup. No API keys required, no external services.

---

## Read this before the numbers

**All recovery outcomes in this project are simulated.** Test-mode APIs
cannot make a synthetic customer decide to pay, so the final "did they pay?"
step is drawn from an explicit probability model in
[`recouper/outcomes/simulate.py`](recouper/outcomes/simulate.py). Every
parameter is documented in that one file with its reasoning, and printable
via `python -m recouper.cli params`.

The contribution here is **the measurement framework and the bounded
execution around it** — not the rupee figure, which is a property of those
parameters and would change if you changed them.

No real customer data is used. No message is ever sent — the notification
adapter is a logged no-op. Synthetic emails use `@example.invalid`, reserved
by RFC 2606, so they cannot route anywhere even if the data escaped.

---

## The idea this is built on

Most recovery systems report **gross** recovery: *"we chased 200 failed
payments and ₹4 lakh came in."*

That number is not a measure of the system. A large share of those customers
would have paid anyway — the system emailed them first and took credit for
the outcome. Gross recovery mostly measures a population's tendency to
self-recover.

So every batch randomly holds out **20% of cases as an untouched control
arm** — no contact, no retry, not even a plan. The headline number is the
difference:

```
incremental recovery = treated_rate − control_rate
```

reported with a bootstrap confidence interval, because a difference between
two noisy proportions is itself noisy.

**On the current corpus that produces:**

| | |
|---|---|
| Treated recovery rate | 19.5% |
| Control (self-recovery) rate | 20.6% |
| **Lift** | **-1.1%, 95% CI [-12.5%, +9.2%] - not significant** |
| Gross recovered | Rs.2,80,505 |
| **Incremental recovered** | **-Rs.13,625** |

Read literally, this batch says the agent achieved nothing. The default seed
is kept exactly as it is: publishing a friendlier one is a single flag away,
and resisting that is the point.

Because the honest response to one batch is not to argue with it but to run
it again. Twenty times, fresh corpus and randomisation each time:

```bash
python -m recouper.cli sensitivity --full-seeds 20
```

| | |
|---|---|
| Mean lift | **+7.0%** (sd 4.9pp) |
| Range across runs | -0.5% to +17.7% |
| Positive in | 19 / 20 runs |
| **Significant in** | **8 / 20 runs** |
| Median overstatement if reporting gross | **2.5x** |

**That is the actual finding.** Not "we recovered Rs.X" but: *a batch this
size cannot demonstrate its own effect more than about 40% of the time.* The
effect is real and positive; a single run is mostly noise, and seed 42
happens to be the worst of twenty.

A conventional demo runs once, keeps a good seed, reports the gross figure
and stops. The distribution above is what that number looks like when you run
it twenty times.

And because those figures rest on a simulator, every conclusion is swept
across a parameter space where each assumption may be wrong by up to a factor
of two:

| Conclusion | Holds in |
|---|---|
| **Gross overstates impact by >=2x** | **86% of draws** |
| The intervention helps at all | 78% of draws |
| One batch shows a >5pp lift | 39% of draws |

The overstatement finding does not depend on the numbers chosen. The rupee
figure does. See [`METRICS.md`](METRICS.md) for the full analysis.

---

## The four ideas

### 1. Not every failure is recoverable, and some are harmful to touch

Retrying a payment that cannot succeed burns gateway fees, annoys customers,
and trips issuer velocity limits that damage approval rates on *good*
traffic. So [`detect/classify.py`](recouper/detect/classify.py) sorts every
failure first:

| Class | Example | What we do |
|---|---|---|
| **Transient** | `gateway_error` | Retry immediately, capped |
| **Deferred** | `insufficient_funds` | Retry at T+3d, T+7d — straddles payday |
| **Re-auth** | `invalid_otp` | No silent retry possible; send a link |
| **Instrument-dead** | `card_expired` | Never retry — 0% success. Ask for a new card |
| **Intent-negative** | `payment_cancelled` | One soft nudge, then stop |
| **Terminal** | `stolen_or_lost_card` | **Hard stop.** No retry, no contact, route to risk |
| **Unclassified** | anything unrecognised | **No automated action.** Exception list |

Two independent conditions must *both* hold before any silent retry: the
failure class must permit one, **and** we must actually hold a reusable
mandate. A payment can be retryable in principle and not retryable by us.

The `Unclassified` bucket is deliberate. A real feed always contains reasons
your taxonomy has not seen; guessing at them is how a recovery system starts
charging cards for reasons it does not understand.

### 2. The LLM proposes; deterministic code disposes

An LLM chooses *how* to approach a case — sequence, timing, wording. It never
decides *whether* an action is permitted. Between proposal and execution sits
[`policy/engine.py`](recouper/policy/engine.py), nine hard rules:

- **Do-not-contact** — absolute, checked first, no override, not even for a ₹90,000 debt
- **Hard stops** — dispute, chargeback, refund, settled, or customer replied → halt permanently
- **3 contacts per debt**, lifetime
- **1 contact per customer per 72h**, across *all* debts (they experience us as one sender)
- **Quiet hours** 21:00–09:00 IST — requeued, never dropped
- **₹50,000 ceiling** → escalate to a human, never auto-act
- **Retry budget**, enforced independently of the classifier
- **Deny by default** on unknown customers

The LLM's output is constrained to a closed action enum, so it cannot propose
something the policy layer has never heard of. Any action outside the allowed
set invalidates the entire plan rather than being silently dropped.

There is deliberately **no `force=True`** anywhere — a bound that can be
bypassed under pressure is documentation, not policy. A test asserts the
signature stays that way.

**That claim is tested, not asserted.** A customer types their own name at
checkout; a gateway writes its own `error_description`. Both are rendered
into the planner's prompt, so both are attacker-controlled:

```bash
python -m recouper.cli redteam
```

26 prompt injections — instruction override, fake system turns, JSON
breakout, unicode smuggling, and plausible-sounding operational notes with no
keywords to filter on — run against a model assumed **fully compromised**. The
stand-in model does not resist anything; it returns exactly the plan the
attacker asked for, every time.

| | |
|---|---|
| Injection attempts | 26 |
| Reached the planner | 26 (all of them, by design) |
| Stopped by schema validation | 7 |
| Stopped by the policy gate | 19 |
| **Policy bypasses** | **0** |

Assuming total compromise is the only version of this test worth running.
Measuring how often Claude resists these prompts would produce a number about
one model on one day; it would look good, and it would change with a version
bump. What the architecture is responsible for is what an attacker gets when
the model gives them everything.

It also means containment is verified **offline, with no API key, on every
commit.**

What the attacker still gets is reported too: attacker text reaches the audit
rationale in 18 of 26 cases. That is not a leak — a ledger that dropped
hostile input would be a worse ledger — but it means anything rendering that
log must treat it as untrusted. The customer-facing message body is
template-derived and never carries it. See [`SECURITY.md`](SECURITY.md).

**Denials are logged as loudly as actions.** A run reports which rules
blocked what:

```
contact_cooldown               153 blocked
do_not_contact                   8 blocked
amount_ceiling                   2 blocked
```

A recovery system whose refusals are invisible cannot be shown to be bounded.

### 3. Failure is handled, not hoped away

```bash
python -m recouper.cli run --no-llm --faults
```

Injects a gateway outage mid-batch:

```
--- BATCH HALTED ---
  10 failures (20%) over the last 50 actions crossed the 20% threshold
  10 cases parked in the DLQ with context.
  Resume is a manual operation -- the breaker does not self-heal.
```

- **Idempotency keys** derived from (case, action, logical attempt), so a
  retry after a *timeout* — where we don't know if the first call landed —
  reuses the key and the provider deduplicates. Random keys are how
  double-charging happens.
- **Exponential backoff with full jitter**, so a recovering gateway doesn't
  get a synchronised thundering herd.
- **Circuit breaker** on failure *rate*, requiring both a minimum
  observation count and a minimum absolute failure count. Retries absorb
  moderate degradation; only a genuine outage halts the batch.
- **Dead letter queue** — work that cannot complete is parked with full
  context, never silently dropped.
- **Manual resume only.** A breaker that self-heals will re-trip against a
  still-broken gateway, sending more traffic each cycle.

---

## Audit trail

Every decision — taken, denied, or escalated — lands in an append-only
JSONL ledger, each record hash-chained to its predecessor.

```bash
python -m recouper.cli verify runs/audit_seed42.jsonl
# chain intact across 576 records
```

Editing, deleting, reordering, or forging a record breaks the chain and
`verify()` reports the exact sequence number. This isn't tamper-*proof* —
anyone who can rewrite the file can recompute the chain — but it makes
casual tampering and accidental corruption detectable, which is the
realistic threat for an operational log.

---

## Running it

```bash
python -m recouper.cli run --no-llm              # full batch
python -m recouper.cli run --no-llm --faults     # outage + breaker demo
python -m recouper.cli redteam                   # 26 prompt injections, 0 bypasses
python -m recouper.cli sensitivity               # do the conclusions survive?
python -m recouper.cli sensitivity --full-seeds 20   # 20 whole runs (slow)
python -m recouper.cli run --json out.json       # machine-readable result
python -m recouper.cli run --html report.html    # visual report
python -m recouper.cli verify runs/audit_seed42.jsonl
python -m recouper.cli params                    # simulation parameters
python -m pytest tests/ -q                       # 174 tests
```

The `--html` report is a single self-contained file with no scripts and no
external assets — it opens offline and renders identically anywhere. Chosen
over a served dashboard deliberately: a report that is just a file has no
ports, no startup race, and no blank-page-while-booting failure mode.

**With an LLM planner** (optional): copy `.env.example` to `.env` and set
`ANTHROPIC_API_KEY`. Without it the system runs the deterministic planner and
marks affected cases `llm_fallback` — a supported mode, not an error. A
component that touches money should work when the model is unavailable.

Everything is seeded. Same seed, same corpus, same numbers.

---

## Layout

```
recouper/
├── providers/     RazorpayClient Protocol + mock; agent code binds only to the Protocol
├── data/          seeded synthetic corpus (955 records)
├── detect/        classification taxonomy + case extraction with dedupe
├── policy/        nine hard rules + the gate
├── agent/         planner (LLM + deterministic fallback), executor
├── outcomes/      the simulation model — every parameter documented
├── audit/         hash-chained ledger
├── eval/          bootstrap CIs, incremental lift, replay + sensitivity analysis
├── redteam/       injection corpus + containment harness
└── cli.py
```

Agent code imports `providers.protocol`, never `mock` or `live`. Swapping in
real test-mode credentials is a one-line change at the composition root.
Entity shapes mirror the real API exactly — integer paise, epoch seconds,
`pay_` / `order_` / `inv_` id prefixes — so the mock cannot drift into being
more convenient than the real thing.

---

## What I deliberately did not build

- **Real money movement.** Test-mode and simulator only.
- **Actually sending email/SMS.** The channel adapter is a logged no-op.
- **A full reason catalogue.** Razorpay's public docs paginate behind an
  index; only the error *schema* and one worked example (`invalid_otp`) were
  reachable. Every rule in the taxonomy is tagged `GROUNDED` or `INFERRED`
  accordingly, and a test asserts we don't overstate the grounding.
- **Individual-level false-positive attribution.** We can estimate how many
  contacts were wasted, but not *which* — that's the fundamental limit of a
  randomised design, not an implementation gap.

---

## Honest limitations

- Outcomes are simulated; the rupee figures inherit whatever the model says.
  The *conclusions* are swept across a wide parameter space
  ([`METRICS.md`](METRICS.md)); the overstatement finding survives, the rupee
  figures do not.
- `ACTION_ODDS_RATIO` is the parameter that most moves the result — roughly
  4× more than `BASE_SELF_RECOVERY`, which an earlier draft of this README
  named as the most consequential. The sweep is how that was found out.
- A 20% control arm on ~340 cases yields wide intervals. Adequate to detect
  large effects, underpowered for small ones — quantified: a batch this size
  reaches significance in 8 of 20 runs.
- The injection corpus is 26 hand-written attacks. Zero escapes is evidence
  about those attacks, not proof about all of them.
- Structural assumptions are untested. Perturbing a parameter cannot tell you
  that odds ratios composing multiplicatively is the wrong functional form.
- Simulation parameters were fixed *before* any recovery number was computed
  and have not been touched since. Git history is the evidence.

---

## Engineering log

[`JOURNAL.md`](JOURNAL.md) is a dated record of what actually went wrong,
kept from the first commit rather than reconstructed at the end. The most
useful entries are the three bugs that broke *data correctness* while
breaking no code path and turning no test red — a fixture a year out of date,
a double-counted denominator, and a batch scheduled at 05:30 that measured
nothing while reporting a plausible null result.

For a project whose entire claim is honest measurement, that pattern is the
main thing I'd want a reviewer to see.
