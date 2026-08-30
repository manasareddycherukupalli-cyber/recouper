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
