# Razorpay AI Buildathon — Track 03: AI Revenue Recovery

**Project:** Recouper — a bounded, auditable revenue-recovery agent
**Applicant:** manasareddycherukupalli@gmail.com · Graduating 2029 (eligible)
**Track:** Track 3 — AI Revenue Recovery (selected in form)
**Written:** 2026-08-30 · **Status:** approved, building

---

## 0. Confirmed facts

### The submission form — RESOLVED

The form is **multi-page**. Page 1 is registration; **page 2 is the project submission**:

| Field | Required | Notes |
|---|---|---|
| Selected Track | ✓ | Track 3: AI Revenue Recovery |
| Project Name / Title | ✓ | short text |
| Project Objectives — *"What does it solve?"* | ✓ | long text — **graded prose** |
| GitHub Repository URL | ✓ | must be public |
| 5-min Pitch Video Link | ✓ | must be publicly viewable |
| Build Challenges & Technical Obstacles — *"What issues did you face while building, and how did you solve them?"* | ✓ | long text — **graded prose** |
| Final Submission Confirmation | ✓ | *"I confirm that this is my official final project submission. I understand that no further changes or edits can be made after submitting."* |

**Two consequences that drive everything below:**

1. **One irreversible shot.** No edits after submit. Nothing gets submitted until repo + video + all prose are final *together*. We rehearse the submission with a filled-out draft before we ever load the real form.
2. **"Build Challenges" is a graded field.** It rewards genuine engineering friction, honestly described. Reconstructing it from memory on the last day produces vague mush. **So: `JOURNAL.md` from commit one** — every real obstacle, what I tried, what actually fixed it, dated. That file is also independent evidence of a real build, which matters when the panel is filtering for authenticity.

### From `razorpay.com/buildathon/` (verified directly)

- Student-only; ₹75,000/month; 6 or 12 months; in-person Bangalore, from September.
- "pick a track, build something real, show your work (a public repo, a 5 minute pitch video, the architecture), and if it has signal we call you in." No aptitude test, no group discussion.
- **Track 03 verbatim:** *"measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."*
- House style across money tracks: *"every money action explainable, bounded and gated. Show the audit trail and one failure handled gracefully."*
- Track 02 adds *"honest metrics including false-positive cost"*; Track 04 adds *"throughput plus measured accuracy plus an honest exception list."* Both bars are worth clearing even though we're on Track 03 — they signal what the reviewers value.
- Runs on **Razorpay test-mode APIs**.

### From Razorpay API docs (verified directly)

- Endpoints: `GET /v1/payments`, `GET /v1/payments/:id`, `POST /v1/payments/:id/capture`, `GET /v1/orders/:id/payments`, `PATCH /v1/payments/:id`, `expand[]=card|emi|offers`.
- Error object: `code`, `description`, `source`, `step`, `reason`, `metadata{payment_id, order_id}`. `source ∈ customer | gateway | bank | network`. Documented example: `BAD_REQUEST_ERROR` / `invalid_otp` / step `payment_authentication` / source `customer`.

### Still open

- **Deadline.** Razorpay's page states none; only job aggregators say "before September 5," single-sourced. We build to Sept 5 defensively. Everything is ordered so that an earlier-than-expected cutoff still finds us with something submittable.

---

## 1. On "100% for sure"

I can't promise an outcome — that depends on the other applicants and on a panel neither of us can see, and anyone telling you otherwise is selling something. What I can do is make the things *within our control* as strong as they go: pick the differentiator most likely to register with payments engineers, build it properly, measure it honestly, and not fumble the presentation.

The realistic edge here is not "more features." A hiring panel reviewing many Track-3 submissions will see a lot of the same demo: chase failed payments, report a big rupee number, ship a dashboard. **The submissions that stand out will be the ones that show judgment.** That's what §2 is built around, and it's why several sections below are about restraint rather than scope.

---

## 2. The thesis — the one idea this submission is built on

Most recovery demos report **gross** money recovered: "we chased 200 failed payments, ₹4L came in." That number is inflated, because a large share of those customers would have retried on their own. A payments company will spot this immediately — it is precisely the mistake their own growth teams get burned by.

> **Only incremental recovery counts.** Every batch splits into a treated arm and a randomised holdout control arm. Headline number is `treated_rate − control_rate` with a confidence interval. Gross recovery is shown alongside, explicitly labelled *"inflated — includes self-recovery."*

Why this is the right bet:

- It directly answers the track's *"measured money recovered"* — with a number that survives scrutiny.
- It delivers Track 02's *"false-positive cost"* bar for free: the control arm literally measures the customers we annoyed for nothing.
- It is **cheap** — a seeded randomiser and an honest metrics module, not a week of engineering.
- It is the kind of thing that makes an interviewer sit up, because it shows you understand what the number *means*, not just how to produce one.

Second pillar: **the agent is bounded by a policy engine it cannot talk its way past.** The LLM proposes; deterministic code disposes. No model output ever directly moves money or contacts a customer.

---

## 3. Scope

### In — three at-risk classes

| Class | Signal | Recovery action |
|---|---|---|
| **Failed payments** | `payment.status = failed`, classified by `error.reason` | Retry (only if retryable + mandate exists), else payment link + nudge |
| **Abandoned checkouts** | `order.status = created/attempted`, no captured payment, aged past threshold | Payment link + bounded nudge sequence |
| **Overdue receivables** | `invoice.status = issued`, past `due_by` | Dunning sequence → human escalation |

### Explicitly out — and said so in the README

- Real money movement. Test-mode / simulator only.
- Anything offence-capable.
- Real customer PII — all synthetic, seeded, reproducible.
- Actually sending email/SMS — channel adapter is a logged no-op behind a swappable interface.

Scope discipline is itself part of the pitch. A tight, complete, well-measured system beats a sprawling half-finished one, and the README saying *"here is what I deliberately did not build, and why"* reads as senior.

---

## 4. Architecture

```
recouper/
├── providers/          Razorpay client boundary
│   ├── protocol.py       RazorpayClient Protocol — the ONLY interface agent code sees
│   ├── mock.py           in-memory simulator, mirrors real SDK shapes exactly
│   └── live.py           thin wrapper over razorpay SDK (test mode) — drop-in
├── data/generate.py      seeded synthetic corpus (400+ records), realistic distributions
├── detect/
│   ├── classify.py       error.reason → retryability taxonomy
│   └── score.py          recoverability score + expected recovery value
├── policy/
│   ├── rules.py          hard bounds: caps, cooldowns, quiet hours, DNC, ceilings
│   └── engine.py         gate(action, ctx) -> ALLOW | DENY(reason) | ESCALATE(reason)
├── agent/
│   ├── plan.py           LLM proposes a recovery plan (structured output, enum-constrained)
│   ├── execute.py        idempotent executor, backoff, circuit breaker, DLQ
│   └── prompts/
├── outcomes/simulate.py  probabilistic response model (LABELLED AS SIMULATION)
├── audit/ledger.py       append-only, hash-chained JSONL
├── eval/
│   ├── backtest.py       treated vs control harness
│   └── metrics.py        incremental lift, bootstrap CIs, cost per recovery
├── api/                  FastAPI
└── web/                  React dashboard
```

**The rule that matters:** agent code imports `providers.protocol`, never `mock` or `live`. Real test-mode keys become a one-line DI change. This forces the mock to mirror real JSON exactly — snake_case, integer paise, epoch seconds, `pay_` / `order_` / `inv_` / `plink_` id prefixes. That fidelity is worth calling out in the video: it's the difference between a toy and something that could actually be pointed at production.

---

## 5. Retryability taxonomy — the core domain logic

Retrying a payment that cannot succeed burns gateway fees, annoys customers, and trips issuer velocity limits. Classification drives everything downstream.

| `error.reason` | Class | Action | Rationale |
|---|---|---|---|
| `payment_failed` (network) | **Transient** | Immediate retry, exp. backoff, max 2 | Instrument is fine |
| `gateway_error` (gateway) | **Transient** | Immediate retry, max 2 | Gateway-side |
| `insufficient_funds` (bank) | **Deferred** | Retry T+3d, then T+7d, cap 2 | Balance is time-dependent; salary-cycle aware |
| `invalid_otp` / auth failure (customer) | **Re-auth** | No silent retry — payment link, customer re-authenticates | Needs a human by definition |
| `card_expired` / `invalid_card` | **Instrument-dead** | Never retry. Link requesting new instrument | Retry success rate is 0% |
| `payment_cancelled` by customer | **Intent-negative** | 1 soft nudge max, then stop | Customer said no |
| `stolen_or_lost_card`, fraud-flagged | **Terminal** | Hard stop, no contact, flag to risk | Contacting is actively harmful |
| unknown / undocumented | **Unclassified** | Exception list, no auto-action | Honest exception list |

The Unclassified bucket is deliberate and surfaced in the dashboard. Claiming 100% reason coverage would be the dishonest choice; an explicit exception list is the credible one — and it directly hits the *"honest exception list"* bar.

**Doc-fidelity note:** reasons confirmed from Razorpay docs are marked as such in code; the rest carry a `# inferred` comment and are listed in the README. Only `classify.py` changes if we later obtain the full catalogue.

---

## 6. Policy engine — "bounded and gated"

Every proposed action passes `gate()`: ALLOW / DENY(reason) / ESCALATE(reason). **Denials are logged as loudly as approvals** — a denial is a feature.

- Max **3** contacts per customer per debt, lifetime.
- Max **1** contact per customer per **72h** across all debts (no multi-debt pile-on).
- Max **2** retries per payment; never for Terminal or Instrument-dead.
- **Quiet hours** 21:00–09:00 IST — queued, not dropped.
- **Do-not-contact** honoured absolutely, checked first, no override path.
- **Amount ceiling:** debts > ₹50,000 → ESCALATE, never auto-actioned.
- **Hard stops:** open dispute, chargeback, refund issued, customer replied, debt settled — any one halts that workflow permanently.
- **Circuit breaker:** >20% action-failure rate over a 50-action window → halt batch, alert, require manual resume.
- **Batch budget:** max actions per run, so a bug cannot fan out.

Each rule is a named, individually unit-tested predicate. The suite includes adversarial cases — LLM proposing contact on a DNC customer, on a disputed debt, at 03:00, above the ceiling — each of which must be denied. **That test file is a demo asset:** the most direct possible evidence of "bounded and gated," and a natural thing to open on camera.

---

## 7. Where the LLM is used — and where it is not

**Used:** plan selection per case (structured output, constrained to an enum of allowed actions, with written rationale); message drafting; exception triage.

**Not used:** classification (deterministic table), policy decisions (an LLM that can be argued out of a bound is not a bound), metrics (arithmetic).

Model `claude-sonnet-5`, every call logged into the audit ledger (prompt hash, response, tokens, latency).

**Fallback:** LLM unavailable or malformed output → deterministic default plan per class, case marked `llm_unavailable`, recovery continues degraded. One of our graceful-failure demos.

This split is a deliberate pitch talking point. Plenty of submissions will wire an LLM into the decision path and call it agentic. Showing you knew where *not* to put it is the stronger signal.

---

## 8. Honest measurement

Every eligible case is seeded-randomised into **treated (80%)** / **control (20%)**. Control gets zero contact, zero retries — we just observe.

| Metric | Definition |
|---|---|
| **Incremental recovery rate** | `treated_rate − control_rate` |
| **Incremental ₹ recovered** | lift × treated cohort value, 95% bootstrap CI |
| **Gross ₹ recovered** | shown alongside, labelled *"inflated — includes self-recovery"* |
| **Self-recovery rate** | control-arm rate — the number most demos silently claim credit for |
| **Cost per ₹ recovered** | (retry fees + message cost + LLM spend) / incremental recovered |
| **Contacts per recovery** | customer-annoyance proxy |
| **False-positive cost** | contacts to customers who self-recovered anyway × unit cost + goodwill flag |
| **Detector precision** | of cases flagged at-risk, share genuinely unrecovered at horizon |
| **Policy denials by rule** | proof the bounds bind |
| **Exception list** | unclassified / escalated / stuck cases, itemised |

**If the CI on incremental lift crosses zero, the dashboard says so.** Reporting "no significant lift on cohort X" reads as far more credible than uniform success — and it gives you something genuinely interesting to say in an interview.

### The simulation caveat — stated loudly, everywhere

Test-mode APIs cannot make a synthetic customer actually pay, so outcomes come from a probabilistic response model. Its parameters (base self-recovery by error class, contact uplift, decay by debt age, fatigue penalty) live in **one documented config file** with a stated basis per number. README, dashboard, and the video all say plainly: *these are simulated outcomes under an explicit, inspectable model; the contribution is the measurement framework and bounded execution, not the absolute rupee figure.*

Passing simulated rupees off as real is the fastest way to fail this track. Owning it is the strong move — and it costs us nothing, because our actual claim was never the rupee number.

---

## 9. Graceful failure — the required demo

The brief asks for "one failure handled gracefully." We implement several and **script one** for the video:

**Primary:** mid-batch, the gateway starts returning 5xx. Executor retries with exponential backoff + jitter → circuit breaker trips at 20% → batch halts cleanly → in-flight actions not double-sent (idempotency keys) → affected cases land in DLQ with full context → audit log shows the trip and reason → dashboard shows halted batch with manual-resume control. Operator resumes; work continues from exactly where it stopped, **zero duplicate customer contacts**.

Also built: LLM timeout → deterministic fallback; malformed LLM output → schema rejection, one retry, then exception list; duplicate webhook → idempotent no-op; quiet-hours boundary → queued not dropped.

---

## 10. Audit trail

Append-only JSONL, each record hash-chained to its predecessor (`prev_hash`) so tampering is detectable. Fields: `ts`, `actor` (system|llm|policy|human), `case_id`, `action`, `decision`, `reason`, `policy_checks[]` (every rule evaluated + outcome), `inputs_hash`, `result`, `idempotency_key`.

Denied and escalated actions logged with equal weight to executed ones. Dashboard renders any case as a full timeline. `verify_chain()` is a CLI command — shown on camera, because a hash chain you can *verify live* is more convincing than one you describe.

---

## 11. Deliverables

1. **Public GitHub repo** — code, tests, seeded generator, one-command run.
2. **`ARCHITECTURE.md`** — diagram, provider-boundary decision, LLM-vs-deterministic split, policy model, failure model, honest "what I'd do with more time."
3. **`METRICS.md`** — results with CIs, exception list, simulation caveat.
4. **`JOURNAL.md`** — dated engineering log; feeds the Build Challenges answer.
5. **5-minute pitch video.**
6. **Dashboard** — batch view, case timeline, policy-denial feed, metrics, live breaker state.

---

## 12. Schedule

Front-loaded so that **by end of Sept 1 there is a working, measured, submittable CLI product.** Dashboard and video are additive. If the deadline proves tighter than expected, we ship what exists — never a half-built dashboard over a working measured agent.

| Day | Deliverable |
|---|---|
| **Aug 30 (today)** | Repo public + skeleton; provider protocol + mock; synthetic generator; classification taxonomy + tests; `JOURNAL.md` started |
| **Aug 31** | Policy engine + adversarial test suite; audit ledger with hash chain |
| **Sep 1** | Agent planner (LLM, structured output), executor, idempotency, backoff, breaker, DLQ → **first end-to-end batch run** |
| **Sep 2** | Outcome simulator, backtest harness, metrics with bootstrap CIs → **METRICS.md v1** |
| **Sep 3** | FastAPI + React dashboard |
| **Sep 4** | Full batch run, ARCHITECTURE.md, README, failure-demo rehearsal, **draft all 3 form answers** |
| **Sep 5** | Record + cut video, final review, submit |

**Buffer rule:** the video is the highest-risk item (recording, re-takes, upload, permissions). If Sept 4 slips, cut dashboard polish — never video time.

---

## 13. Pitch video — 5 min

Judges watch many of these. Front-load the differentiator; do not spend the first minute on generic problem framing.

- **0:00–0:30** — The problem *and* the honest framing: most recovery tools report gross recovery; here's why that number lies. (Hook first.)
- **0:30–1:15** — Architecture in 60 seconds, one diagram.
- **1:15–2:15** — Live batch: detection → classification → plan → policy gate → execution.
- **2:15–3:00** — **A denial.** Agent proposes a contact; policy engine refuses it; the rule is named on screen. The money shot for "bounded and gated."
- **3:00–3:45** — Gateway fails. Breaker trips. Clean halt, no duplicate sends, resume.
- **3:45–4:40** — Metrics: incremental vs gross, the CI, false-positive cost, exception list. Simulation caveat stated out loud.
- **4:40–5:00** — What I'd build next; what I'd need real data to answer.

Production notes: script it, do not improvise. Clean desktop, large fonts, no notification popups. Upload unlisted-but-public YouTube; **verify the link in an incognito window** before it goes in the form.

---

## 14. Form answers — drafted early, finalised last

Drafted on Sept 4, finalised Sept 5, into a local `SUBMISSION.md` first. Because submission is irreversible, we paste from a reviewed file — we never compose in the form.

- **Project Name / Title** — plain and descriptive over clever.
- **Project Objectives ("What does it solve?")** — lead with the incremental-vs-gross insight; state the three at-risk classes; state the bounded-execution guarantee; state the simulation caveat in one clause.
- **Build Challenges & Technical Obstacles** — sourced from `JOURNAL.md`. Real obstacles with real resolutions: idempotency under retry-plus-breaker interaction, control-arm contamination, bootstrap CI on a small cohort, constraining LLM output to a safe action enum. Specific and technical; no "time management was hard."

### Pre-submit checklist (all must pass before the checkbox)

- [ ] Repo public — verified in a logged-out browser
- [ ] `git clone` → one documented command → runs clean on a fresh machine
- [ ] README opens with what it is, what's simulated, how to run
- [ ] Video link plays logged-out; length ≤ 5:00
- [ ] All three prose answers reviewed in `SUBMISSION.md`
- [ ] No secrets in git history (`.env` ignored from commit one)
- [ ] Track = Track 3
- [ ] Then, and only then, tick confirmation and submit

---

## 15. Open items for you

1. **Deadline** — if you can find or confirm a real date, tell me; the schedule adjusts.
2. **Anthropic API key** — needed for the planner. Without it I build the deterministic fallback first and wire the LLM last, which is sound ordering regardless.
3. **GitHub username** — for the repo URL.
4. **Repo public from commit one?** Recommended: yes. The commit history becomes evidence of a genuine week-long build rather than a single dump.
