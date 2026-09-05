/**
 * The same palette as the walkthrough page and the operator console, so the
 * video is recognisably the same product rather than a third look.
 *
 * Copied value-for-value from recouper-demo.html. Video renders on a fixed
 * background, so only the dark set is used here -- a rendered frame has no
 * viewer theme to follow.
 */

export const c = {
  ground: "#0E1414",
  surface: "#161E1D",
  surfaceSunk: "#111918",
  ink: "#E3EAE8",
  inkSoft: "#9AA8A4",
  inkFaint: "#6B7976",
  rule: "#283433",
  ruleSoft: "#1E2827",

  petrol: "#5FBDB4",
  petrolBg: "#14302E",

  // The policy engine's three verdicts. Reserved -- nothing decorative may
  // borrow them, because on screen they have to mean exactly one thing.
  allow: "#6FBF92",
  allowBg: "#15291F",
  deny: "#E8877A",
  denyBg: "#301A17",
  escalate: "#DCAB4E",
  escalateBg: "#2C2313",
} as const;

/** Real figures from `python -m recouper.cli run --no-llm`, seed 42. */
export const data = {
  records: 955,
  cases: 340,
  byKind: [
    { label: "failed payments", n: 180 },
    { label: "abandoned checkouts", n: 90 },
    { label: "overdue invoices", n: 70 },
  ],
  deduped: 180,
  treated: 277,
  control: 63,
  plansCreated: 277,
  executed: 133,
  denied: 161,
  escalated: 2,
  denials: [
    { rule: "contact_cooldown", n: 153 },
    { rule: "do_not_contact", n: 8 },
    { rule: "amount_ceiling", n: 2 },
  ],
  ledgerRecords: 576,
  treatedRate: 0.1949,
  controlRate: 0.2063,
  lift: -0.0114,
  ciLow: -0.1254,
  ciHigh: 0.0918,
} as const;

/** Evaluated in this order, and all nine are evaluated every time. */
export const rules = [
  "do_not_contact",
  "unknown_customer",
  "hard_stop",
  "class_allows_contact",
  "amount_ceiling",
  "contact_cap_per_debt",
  "contact_cooldown",
  "quiet_hours",
  "retry_eligibility",
] as const;

export const classes = [
  { name: "Transient", eg: "gateway_error", retry: true, contact: true },
  { name: "Deferred", eg: "insufficient_funds", retry: true, contact: true },
  { name: "Re-auth", eg: "invalid_otp", retry: false, contact: true },
  { name: "Instrument-dead", eg: "card_expired", retry: false, contact: true },
  { name: "Intent-negative", eg: "payment_cancelled", retry: false, contact: true },
  { name: "Terminal", eg: "stolen_or_lost_card", retry: false, contact: false },
  { name: "Unclassified", eg: "anything unrecognised", retry: false, contact: false },
] as const;
