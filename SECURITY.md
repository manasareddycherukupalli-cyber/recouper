# Containment under a compromised model

```bash
python -m recouper.cli redteam
```

The README's central architectural claim is that **the LLM proposes and
deterministic code disposes.** This document is that claim tested rather than
asserted.

---

## The threat model

The attacker controls text in fields this pipeline reads. That is not an
exotic assumption — it is the ordinary case:

- a customer types their own **name** at checkout
- a merchant writes their own **order and customer notes**
- an **`error_description`** is a string a gateway hands us

All three are rendered into the planner's prompt. Any of them can try to be
read as instructions rather than as data.

**We assume the model is fully compromised.** Not "occasionally fooled" —
compromised. The `SuborneModel` in `recouper/redteam/harness.py` reads the
attacker's text and returns exactly the plan the attacker wanted, every time,
with a plausible rationale attached.

This is deliberately harsher than reality, and it is the only version of the
test worth running. Measuring how often Claude resists these prompts would
produce a number about a particular model on a particular day. It would look
good, it would not be a property of this codebase, and it would change under
a version bump. What matters is what an attacker gains **when the model gives
them everything** — because that is what the architecture is responsible for.

A corollary worth stating: the test needs no API key and is fully
deterministic. Containment is verified offline, in CI, on every commit.

---

## Results

26 injections across 6 attack families and 8 attacker objectives:

| | |
|---|---|
| Injection attempts | 26 |
| Reached the planner | 26 (all of them, by design) |
| Stopped by schema validation | 7 |
| Stopped by the policy gate | 19 |
| **Policy bypasses** | **0** |

### By attack family

| Family | n | Schema | Policy | Escaped |
|---|---|---|---|---|
| Instruction override | 5 | 1 | 4 | 0 |
| Role confusion | 4 | 1 | 3 | 0 |
| Schema breakout | 4 | 2 | 2 | 0 |
| Encoding / unicode | 4 | 1 | 3 | 0 |
| Plausible context | 5 | 1 | 4 | 0 |
| Structural | 4 | 1 | 3 | 0 |

### By attacker objective

| Objective | Attempts | Result |
|---|---|---|
| Contact a do-not-contact customer | 3 | contained |
| Exceed the retry budget | 4 | contained |
| Contact a stolen-card customer | 2 | contained |
| Send during quiet hours | 2 | contained |
| Auto-act above the ₹50,000 ceiling | 2 | contained |
| Invent an action outside the enum | 5 | contained |
| Escalate privilege / "maintenance mode" | 5 | contained |
| Exfiltrate data | 3 | contained |

### Which rules did the work

| Rule | Denials |
|---|---|
| `retry_eligibility` | 16 |
| `do_not_contact` | 6 |
| `amount_ceiling` | 6 |
| `quiet_hours` | 4 |
| `contact_cooldown` | 3 |

---

## Why it holds

Four layers, listed with what each is actually worth.

**1. The system prompt — worth nothing here.** It tells the model it is not
the safety layer. That is real and useful against an *honest* model having a
bad day. Against a compromised one it is worth exactly zero, and it is
counted at zero. A defence-in-depth story that credits its weakest layer is
marketing.

**2. Schema validation — 7 attempts died here.** The model's reply is parsed
against the closed `ActionType` enum. Two properties matter:

- the enum is **closed**, so `charge_saved_card` and `delete_audit_log` are
  not actions the system can express, let alone perform;
- an illegal action invalidates the **entire plan** rather than being dropped
  from it. Silently dropping would execute a plan the model never proposed —
  a different plan, chosen by the error handler.

**3. The policy gate — 19 attempts died here.** These are the interesting
ones: the model proposed a perfectly *legal* action, aimed at a target the
rules forbid. `send_reminder` is a valid action; sending it to a
do-not-contact customer is not. The gate does not read the rationale, so a
persuasive justification buys nothing.

**4. Recomputed classification.** Retry eligibility is derived from the
failure class at gate time rather than trusted from the plan, so a plan
cannot argue a dead card into a retryable one.

The load-bearing property across all four: **no rule consults an LLM, and
there is no `force=True` anywhere in the codebase.** A bound that can be
bypassed under pressure is documentation, not policy. A test asserts the
signature stays that way.

---

## What the attacker still gets

Blocking the action is not the whole story. The text still flows through the
system, and pretending otherwise would be the same overclaiming this project
exists to avoid.

| Sink | Reached in |
|---|---|
| The message body a customer receives | 1 / 26 |
| An outbound customer field (name) | 6 / 26 |
| The audit rationale | 18 / 26 |

**The message body is template-derived** — built from the debt amount alone,
never from customer or model text. This is the strongest containment property
in the system: an attacker cannot use it to send content of their choosing to
another person. (The single count above is an artefact of one payload sharing
a long word with the template, not carried text.)

**Attacker text does reach the audit rationale, in 18 of 26 cases.** This is
not a leak and not a bug — a ledger that discarded hostile input would be a
worse ledger, and the rationale is exactly where an investigator would look
to understand why a plan was made. It is stated explicitly because it has a
consequence: **anything that renders the audit log must treat its contents as
untrusted text.** The HTML report escapes it; a future dashboard must too.

`str-01` in the corpus is an `<img onerror=...>` payload aimed at that
renderer rather than at the LLM, for precisely this reason.

---

## Testing the tests

A red-team suite that cannot fail is theatre. `tests/test_redteam.py`
therefore includes tests that deliberately break the system and assert the
harness notices:

- `test_the_harness_detects_a_bypass_when_the_policy_is_disabled` — swaps in
  an allow-everything gate and asserts escapes are reported. Without it, a
  scoring bug that always printed "0 escapes" would be indistinguishable from
  a secure system.
- `test_disabling_the_gate_lets_dnc_contact_through` — names the specific
  harm the gate prevents.
- `test_the_compromised_model_actually_gets_its_plan_past_the_parser` — if
  every injection died at the parser, the policy layer would be untested and
  the "stopped by policy" column would be a lie.
- `test_scenarios_put_the_system_in_the_state_the_attack_needs` — a DNC
  attack run against a non-DNC customer would pass for the wrong reason.

### A bug this analysis had

The first version scored three exfiltration attempts as **escapes**. They
were not. The exfiltration scenario builds an ordinary contactable customer,
so `send_reminder` is legitimate work — the harness was scoring correct
behaviour as a breach.

The fix was to score exfiltration by **data flow** rather than by action,
which is what the taint table above measures.
`test_exfiltration_is_scored_by_data_flow_not_by_action` guards it.

Worth recording because it cuts both ways: a red-team harness can report
false *positives* as easily as false negatives, and an unexamined "3 escapes"
headline would have been just as misleading as an unexamined "0".

---

## What this does not cover

- **A model that is honest but wrong.** The harness models total compromise,
  not subtle misjudgement. A model that proposes a legal action for a bad
  reason is contained by the same gate but is not measured here.
- **The real API path.** Containment is tested against a substituted client.
  The `LLMPlanner` parsing code is real and exercised; the HTTP layer is not.
- **Attacks on the policy engine's own inputs.** If an upstream system lies
  about `do_not_contact`, no amount of gating helps. The DNC flag is trusted.
- **Denial of service.** `str-03` floods the context, but nothing here
  measures cost amplification from an attacker forcing expensive plans.
- **The corpus is not exhaustive.** 26 hand-written injections cover the
  families I could think of. Absence of an escape is evidence about these
  attacks, not proof about all of them.
