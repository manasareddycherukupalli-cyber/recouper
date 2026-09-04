# Architecture

## The pipeline

```
  Razorpay test-mode API  (or the in-memory simulator)
            │
            ▼
  ┌─────────────────────┐
  │  detect/score.py    │  extract cases, dedupe by precedence
  │  detect/classify.py │  sort each failure into a recovery class
  └─────────┬───────────┘
            │  340 cases
            ▼
  ┌─────────────────────┐
  │   runner.py         │  seeded random split
  └────┬───────────┬────┘
       │ 80%       │ 20%
       ▼           ▼
   TREATED      CONTROL ──────────┐   never touched:
       │                          │   no plan, no contact, no retry
       ▼                          │
  ┌─────────────────┐             │
  │  agent/plan.py  │  LLM proposes a sequence
  └────────┬────────┘  (constrained to a closed enum)
           │                      │
           ▼                      │
  ┌─────────────────┐             │
  │ policy/engine.py│  ◄── THE GATE. 9 deterministic rules.
  └────┬───────┬────┘      ALLOW / DENY / ESCALATE
       │       │                  │
   ALLOW    DENY/ESCALATE         │
       │       └──────────┐       │
       ▼                  │       │
  ┌─────────────────┐     │       │
  │agent/execute.py │     │       │  idempotency, backoff,
  └────────┬────────┘     │       │  circuit breaker, DLQ
           │              │       │
           ▼              ▼       ▼
  ┌──────────────────────────────────┐
  │      audit/ledger.py             │  hash-chained, append-only
  │  actions AND refusals, equally   │
  └──────────────────────────────────┘
           │
           ▼
  ┌──────────────────────────────────┐
  │  outcomes/simulate.py            │  SIMULATED outcomes
  │  eval/metrics.py                 │  treated − control, bootstrap CI
  └──────────────────────────────────┘
```

---

## The five decisions that shaped this

### 1. The provider boundary came first

`providers/protocol.py` was written before any agent logic existed.

The tempting order is to build the interesting part and abstract the API
later. That reliably bakes mock-shaped assumptions into business logic — you
discover at integration time that your code assumes floats for amounts or
datetimes for timestamps, and the "one-line swap" becomes a rewrite.

So `entities.py` mirrors the live API exactly: **integer paise** (never
floats — binary floating point cannot represent ₹0.01 exactly, and money
arithmetic that drifts is worse than money arithmetic that fails), **epoch
seconds**, snake_case field names, `pay_` / `order_` / `inv_` / `plink_` id
prefixes.

Agent code imports `providers.protocol` and receives a client by injection.
Nothing in `detect/`, `policy/`, `agent/` or `eval/` imports `mock` or `live`.
Swapping in real test-mode credentials is one line at the composition root.

**Trade-off:** more ceremony up front, and the mock must be maintained to
match the real API. Worth it — the alternative is discovering the mismatch
under deadline.

### 2. The LLM is untrusted by construction

The split:

| Deterministic | LLM |
|---|---|
| Classification (`classify.py`) | Plan selection |
| Policy decisions (`policy/`) | Message drafting |
| Metrics (`eval/`) | Exception triage |

The reasoning: **a bound an LLM can be argued out of is not a bound.** If the
model can emit `"contact this customer"` and that string causes a contact,
then prompt phrasing is a security boundary. Prompt phrasing should never be
a security boundary.

Three mechanisms enforce this:

1. **Closed action enum.** The model chooses from `ActionType`; it cannot
   propose `offer_discount` because that isn't an option it is given. Safety
   comes from the closed set, not from asking nicely in a prompt.
2. **Schema rejection, not repair.** Any action outside the allowed set
   invalidates the *entire* plan. Silently dropping the bad element would
   execute a plan the model did not propose.
3. **The gate runs regardless.** Every action is checked whether it came from
   the LLM or the fallback table.

**The fallback is not an afterthought.** With no `ANTHROPIC_API_KEY` the
deterministic planner runs and cases are marked `llm_fallback`. A component
that touches money must work when the model is unavailable.

**Trade-off:** the LLM contributes less than a fully agentic design. That's
the intent. Its judgment is applied where judgment helps — weighing debt age
against amount against history — and withheld where correctness is required.

**This is verified, not assumed.** `redteam/` runs 26 prompt injections
against a model stubbed to be *fully compromised* — it returns exactly what
the attacker asked for, every time. 7 attacks die at schema validation, 19 at
the policy gate, 0 get through. Testing against a compromised stub rather
than the real model is deliberate: containment then becomes a property of
this codebase, verifiable offline with no API key on every commit, rather
than a property of one model version. See [`SECURITY.md`](SECURITY.md).

### 3. Deny by default, with no override

Nine rules in `policy/rules.py`, each a named, individually-tested predicate.
Three composition decisions:

**All rules evaluate — no short-circuiting.** Returning on the first denial
means an operator fixes it, re-runs, and finds the next one, one deploy at a
time. A verdict carries every rule's result.

**DENY outranks ESCALATE.** Both can fire — a ₹90,000 debt for a
do-not-contact customer trips both. If escalation won, a human would be shown
a DNC-violating contact and asked to approve it, converting a hard compliance
rule into a prompt for someone having a bad day. Refusing is always safe.

**Timing denials are distinguishable from refusals.** Quiet hours and
cooldowns are mistimings — requeue them. Everything else is a prohibition;
requeuing it would produce the same denial forever. `is_retryable_later`
tests whether the blocking set is a *subset* of the timing rules, so an
action blocked by both quiet hours and DNC never requeues.

**There is no `force=True`.** A test asserts `gate()`'s signature stays
single-argument, to catch the inevitable "just add an override for the urgent
case" patch.

**Trade-off:** conservative bounds leave recoverable money on the table. The
asymmetry justifies it — a missed recovery costs one debt; a wrongly-sent
contact costs a relationship and possibly a compliance breach.

### 4. Two-condition retry gate

A silent retry requires **both**:

1. the failure class permits one (`insufficient_funds` yes, `card_expired`
   never), **and**
2. we actually hold a reusable mandate.

Collapsing these into one boolean is the natural first implementation and it
produces phantom retries: `insufficient_funds` is textbook-retryable, but
with no stored instrument there is nothing to charge.

Enforced in **three** places — the classifier, the policy engine, and the
mock provider. Deliberate redundancy: the layer that moves money should not
depend on an upstream module having been correct.

### 5. Deduplication belongs in extraction, not the entity

An order carrying a failed payment satisfies `Order.is_abandoned` — status
`attempted`, `amount_paid = 0`. Both properties are true; the *cases*
overlap.

`extract_cases()` applies precedence: a failed payment is a failed-payment
case; an order is an abandoned-checkout case only if no payment was ever
attempted. On the current corpus this removes 180 duplicates from 520 down to
340 — without it every recovery rate is deflated ~35%.

`Order.is_abandoned` is left alone. The property tells the truth about an
order; the dedupe belongs where cases are built.

---

## Failure model

| Failure | Response |
|---|---|
| Gateway 5xx / timeout | Backoff with full jitter, ≤3 transport retries, same idempotency key |
| Rate limit (429) | Honour the server's `retry_after` over our own schedule |
| Permanent 4xx | No retry — straight to DLQ |
| Sustained outage | Circuit breaker halts the batch |
| LLM unavailable / malformed | Deterministic fallback, case marked, work continues |
| Unknown `error.reason` | Exception list, no automated action |
| Unknown customer | Deny by default |
| Quiet-hours boundary | Requeue, never drop |
| Crash mid-batch | Ledger resumes the chain from the tail |

**Idempotency keys** derive from `(case_id, action, logical_attempt)` —
deterministic, so all transport retries of one logical attempt share a key.
The failure this prevents: a call times out *after* the provider accepted it,
we retry, and the customer is charged twice. Random keys make every retry a
fresh operation, which is exactly how that happens.

**Full jitter** (uniform over `[0, cap]`) rather than fixed delay plus wobble,
because a synchronised retry herd causes the outage it is recovering from.

**The breaker needs two floors**: a minimum observation count *and* a minimum
absolute failure count. Rate alone tripped on two failures over a partially
filled window. It also does not self-heal — an auto-closing breaker re-trips
against a still-broken gateway, sending more traffic each cycle. Resume is a
human act.

---

## Audit trail

Append-only JSONL, each record hash-chained via `prev_hash`. `verify()`
detects edits, deletions, reordering and forged appends, reporting the exact
sequence number.

This is **not tamper-proof** — anyone who can rewrite the file can recompute
the chain. It makes casual tampering and accidental corruption *detectable*,
which is the realistic threat for an operational log. Real tamper-resistance
needs an append-only store or external anchoring; noted as a limitation
rather than overclaimed.

Writes are flushed and `fsync`'d before in-memory state updates, so a crash
can never leave us believing we logged something we didn't. Losing a log tail
is recoverable; a log claiming a customer was contacted when they weren't is
not.

JSONL over a database: append-only by nature, survives a crash mid-write
without corrupting earlier records, and readable by a reviewer who doesn't
want to run our code.

**576 records for 340 cases** — roughly half the ledger is decisions *not* to
act.

---

## Measurement

`eval/metrics.py` is where the project's claim lives.

Every batch splits seeded-randomly into treated (80%) and control (20%).
Control receives nothing — no plan, no contact, no retry. Headline number is
`treated_rate − control_rate` with a **percentile bootstrap** CI.

Bootstrap over a normal approximation because the control arm is small by
construction and several cohort rates sit near zero, where a symmetric
interval extends into impossible territory. Resampling makes no
distributional assumption.

**Assignment is random, not alternating.** The case list is sorted by
expected value, so any systematic rule would load one arm with higher-value
cases and bias the comparison from the outset.

**Incremental rupees use the treated arm's *mean* case value**, not the mean
of recovered cases. The lift is a cohort-level rate; the recovered subset is
self-selected toward whatever amounts correlate with recovery, and mixing the
two populations would inflate the result.

**Negative lift is reported, not clamped.** Badly-timed dunning can push a
wavering customer into cancelling. A framework that floors at zero conceals
exactly the finding an operator most needs.

### Execution and observation are separate phases

`eval/replay.py` splits a run in two: the agent works cases and records a
`CaseTrace` per case, then outcomes are drawn from those traces. Nothing in
`policy/`, `agent/` or `detect/` ever reads a recovery probability, so the
agent's behaviour is genuinely independent of the outcome model.

That independence is what makes `eval/sensitivity.py` meaningful. It replays
one fixed batch under thousands of parameter sets, so any variation in the
reported metrics is attributable to the assumptions alone — re-running the
whole pipeline per draw would vary the agent's behaviour too and confound the
two.

Each case carries one fixed uniform draw derived from `(case_id, seed)`
rather than a position in an RNG stream. Two consequences: comparing
parameter sets compares the same sample rather than two different ones
(without this, small real differences vanish into Monte Carlo noise), and a
halted or resumed batch reproduces identical outcomes regardless of ordering.

Outcomes are drawn once for both arms together at the end of the batch rather
than inside the treated loop, so the two arms cannot occupy different
positions in a random stream — a subtle way to bias a comparison that is
supposed to differ only by treatment.

---

## What I'd do with more time

- **Replace the outcome simulation with real observed recovery.** Removes the
  caveat governing every number in `METRICS.md`. Everything else is
  scaffolding around this.
- **Sequential testing.** Currently one batch, one measurement. Real dunning
  runs continuously, so the right tool is a sequential test with
  alpha-spending rather than repeated peeking at a fixed-horizon CI.
- **Per-cohort budget allocation.** The per-class table already shows which
  cohorts respond. A controller could shift spend toward them automatically —
  a bandit over cohorts, with the control arm preserved.
- **Real tamper-resistance** — append-only storage or external anchoring
  rather than a self-verifying chain.
- **Churn follow-up.** A recovery that costs the customer relationship isn't
  a recovery, and nothing here would currently detect it.
- **Load the full `error.reason` catalogue** and shrink the inferred half of
  the taxonomy.

---

## Known limitations

1. Outcomes are simulated; rupee figures inherit the model's parameters.
2. The 21-day age half-life is a guess and consequential.
3. A 20% control arm on 340 cases yields wide intervals — adequate for large
   effects, underpowered for small ones, as the headline result shows.
4. Roughly half the classification taxonomy is inferred rather than
   documentation-grounded, tagged as such in code.
5. Individual-level false-positive attribution is impossible by design — we
   can estimate how many contacts were wasted, not which.
6. Single-process, in-memory contact history. Distributing this would need
   shared state, and the cooldown rule would become a distributed-locking
   problem.
