# Engineering journal

Dated log of real obstacles and how they were resolved. Kept from commit one
rather than reconstructed at the end, because reconstructed problem-solving
reads like it was reconstructed.

---

## 2026-08-30 — Day 1

### Set up the provider boundary before anything else

Wrote `providers/protocol.py` first, before any agent logic existed. The
temptation was to start with the fun part (the recovery agent) and abstract
the API later. Doing it in that order almost always bakes mock-shaped
assumptions into business logic — you discover at integration time that your
code assumes floats for amounts, or datetimes for timestamps, and the "one
line swap" turns into a two-day rewrite.

Concretely, the fidelity rules that would have been violated: Razorpay
returns amounts as **integer paise**, timestamps as **epoch seconds**, and
ids with `pay_` / `order_` / `inv_` prefixes. Encoded all three in
`entities.py` with a comment explaining why, so the mock cannot drift into
being more convenient than the real thing.

### Deciding what the LLM is *not* allowed to do

Spent real time on this rather than defaulting to "agent calls LLM, LLM
decides." Settled on: classification and policy are deterministic tables;
the LLM only chooses between actions that are *already legal*.

The reasoning that decided it: a policy bound an LLM can be argued out of is
not a bound. If the model can emit "contact this customer" and that string
causes a contact, then prompt phrasing is a security boundary — which it
should never be. So `policy.gate()` sits between proposal and execution and
knows nothing about the model.

### Two-condition retry gate — caught a phantom-retry bug in design

Initially had a single `retryable` boolean on each classification. Realised
while writing the tests that this conflates two independent facts:

1. does this **failure class** admit a silent retry, and
2. do we actually **hold a reusable instrument** to charge?

`insufficient_funds` is a textbook deferred-retry case, but with no stored
mandate there is nothing to charge — a retry would be a phantom action
against an instrument we do not have. Split it into `recovery_class` and
`has_reusable_instrument`, with `can_retry_silently` as the conjunction.
`test_retryable_class_without_instrument_cannot_be_retried` pins it.

### Being honest about documentation grounding

Could not reach Razorpay's full catalogue of `error.reason` values — the
public docs paginate behind an index, and only the error *schema* plus one
worked example (`invalid_otp` / `payment_authentication` / `customer`) were
retrievable.

Options were: guess the full list silently, or mark what is grounded. Chose
the latter — every rule in `classify.py` carries a `Grounding.GROUNDED` or
`Grounding.INFERRED` tag, and `coverage_report()` reports the split. A test
asserts `inferred > 0` so we cannot quietly overstate our grounding later.

Corollary: any reason arriving at runtime that is not in the table goes to
`UNCLASSIFIED` and takes **no automated action**. Guessing at an unknown
failure reason is how a recovery system starts charging cards for reasons it
does not understand.

### Corpus generation exposed a double-counting bug

First generator run reported 180 failed payments and **270** abandoned
orders, from only 90 intentionally-abandoned ones. Cause: an order carrying a
failed payment satisfies `Order.is_abandoned` too — status `attempted`,
`amount_paid == 0`.

Semantically both properties are correct, but the *cases* overlap: an order
with a failed payment is a failed-payment case, not an abandoned-checkout
case. Left uncorrected this would have inflated the recovery denominator by
~150 phantom cases and made the headline recovery rate meaningless.

Fix goes in detection rather than in the entity: case extraction applies
precedence — failed-payment first, and an order is only an abandoned-checkout
case if **no payment was ever attempted against it**. Deliberately not
"fixing" `is_abandoned`, because the property is telling the truth; the
dedupe belongs where cases are built.

Wider lesson worth keeping: this is a metrics bug, not a crash. Nothing would
have thrown. It would have silently produced a better-looking number, which
is the most dangerous class of bug in a project whose entire claim is honest
measurement.

### Distribution parameters declared, not tuned

Failure-reason weights in `generate.py` are estimates and are labelled as
such. The rule I am holding myself to: never adjust a corpus parameter
*after* seeing what it does to the recovery number. A dataset quietly tuned
to flatter the agent would invalidate the whole submission.

Included two reasons (`unknown_reason_bank_ref_401`, `issuer_unavailable`)
that are deliberately outside the taxonomy, so the exception path is
exercised on every run. A real feed always contains reasons your taxonomy has
not seen; a corpus without any is not a fair test.

Also used `@example.invalid` for synthetic emails — RFC 2606 reserves it, so
it cannot route to a real inbox even if this data escaped the sandbox.

**Status end of day 1:** entities, provider protocol, taxonomy + 17 passing
tests, seeded 955-record corpus.

---

## 2026-08-30 — Day 2

### Policy engine: deny-by-default and no override path

Built the nine rules. Two composition decisions took the most thought:

**All rules evaluate, always — no short-circuiting.** The obvious
implementation returns on the first denial. But then an operator fixes that
denial, re-runs, and discovers the next one, one deploy at a time. The
verdict now carries every rule's result, so a blocked action reports the
complete picture.

**DENY outranks ESCALATE.** These can both fire — a Rs.90,000 debt for a
do-not-contact customer trips both. If escalation won, a human would be shown
a DNC-violating contact and asked to approve it, which converts a hard
compliance rule into a prompt for someone having a bad day. Refusing is
always safe. `test_deny_outranks_escalate` pins it.

Deliberately no `force=True` anywhere; `test_there_is_no_override_parameter`
asserts `gate()`'s signature stays single-argument, to catch the future "just
add an override for the urgent case" patch.

### Cooldown had to be scoped to the person, not the debt

Wrote the per-debt contact cap (3 lifetime) first, then realised it permits a
customer with four overdue invoices to receive twelve messages — each debt
independently under its cap. The customer experiences us as one sender, so
the cooldown is keyed on `last_contact_ts` at the customer level, across all
debts. `test_cooldown_is_scoped_to_the_person_not_the_debt` covers it.

### Quiet-hours denials must be distinguishable from refusals

Initially all denials were equivalent. That silently loses every recoverable
debt that surfaces overnight: the action is not *wrong*, only mistimed.

Added `GateVerdict.is_retryable_later`, true only when the blocking set is a
subset of the timing rules (`quiet_hours`, `contact_cooldown`). The subset
check matters — an action blocked by both quiet hours *and* do-not-contact
must not be requeued, or we would retry a DNC violation every morning
forever.

### A fixture that was wrong in a way the tests could not see

Verified the hardcoded epochs in the policy tests rather than trusting them.
`NOON_IST` and `THREE_AM_IST` resolved to the right *hours* — 14:00 and 02:10
IST — but in **2025, not 2026**. `DEFAULT_NOW` for the whole corpus was a year
out too.

All 55 tests passed regardless, because the quiet-hours rule only reads the
hour. So the suite was completely blind to it, while every synthetic
timestamp in the corpus was dated to the wrong year.

Two takeaways, both now encoded:

1. Computed the replacements with `datetime` instead of by hand, and left a
   comment saying why the constant is what it is.
2. Added `test_fixture_timestamps_are_when_they_claim_to_be`, which asserts
   the full year/month/day/hour rather than the behaviour they produce. A
   fixture the suite cannot check is worse than no fixture — it manufactures
   false confidence.

This is the second bug in two days that broke *data correctness* while
breaking no code path. Both were found by checking outputs against
expectations by hand rather than by a test going red. Worth remembering when
the metrics land: green tests will not tell me the numbers are right.

**Status end of day 2:** policy engine + 39 adversarial tests (56 total).
