"""Command-line entry point.

    python -m recouper.cli run            # full batch, honest metrics
    python -m recouper.cli run --faults   # gateway outage + breaker demo
    python -m recouper.cli sensitivity    # do the conclusions survive the assumptions?
    python -m recouper.cli redteam        # prompt-injection containment report
    python -m recouper.cli verify         # verify the audit chain
    python -m recouper.cli params         # print the simulation parameters
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent.execute import CircuitBreaker, Executor
from .agent.plan import DeterministicPlanner, LLMPlanner
from .audit.ledger import AuditLedger
from .data.generate import DEFAULT_NOW, generate
from .eval import sensitivity as sens
from .outcomes.simulate import OutcomeModel, model_parameters
from .providers.mock import FaultConfig, MockRazorpayClient
from .runner import BatchConfig, BatchRunner

RUNS = Path("runs")


def _rupees(paise: int) -> str:
    return f"Rs.{paise / 100:,.0f}"


def cmd_run(args: argparse.Namespace) -> int:
    corpus = generate(seed=args.seed, now=DEFAULT_NOW)

    # Severity matters, and the default is deliberately high.
    #
    # At a moderate per-call failure rate (~0.45), three transport retries
    # absorb the outage: only ~0.45^3 = 9% of actions exhaust, which is below
    # the breaker's 20% threshold, so the batch correctly rides it out. That
    # is the intended behaviour -- a breaker that trips on degradation the
    # retries can handle would halt batches unnecessarily.
    #
    # Demonstrating the breaker therefore requires a genuine outage, where
    # retries cannot help. Hence 0.9.
    faults = (
        FaultConfig(
            transient_failure_rate=args.fault_rate,
            healthy_calls=args.healthy_calls,
            seed=5,
        )
        if args.faults
        else FaultConfig()
    )
    client = MockRazorpayClient.from_corpus(corpus, faults=faults)

    RUNS.mkdir(exist_ok=True)
    ledger_path = RUNS / f"audit_seed{args.seed}{'_faults' if args.faults else ''}.jsonl"
    if ledger_path.exists():
        ledger_path.unlink()
    ledger = AuditLedger(ledger_path)

    planner = DeterministicPlanner() if args.no_llm else LLMPlanner()
    if not args.no_llm and not getattr(planner, "available", False):
        print("! ANTHROPIC_API_KEY not set -- using the deterministic planner.\n"
              "  This is a supported mode, not an error.\n", file=sys.stderr)

    executor = Executor(
        client=client, ledger=ledger,
        breaker=CircuitBreaker(threshold=0.20, window=50,
                               min_observations=10, min_failures=5),
        sleep=lambda s: None,  # no real waiting in a demo run
    )
    runner = BatchRunner(
        client=client, ledger=ledger, executor=executor, planner=planner,
        outcomes=OutcomeModel(seed=args.seed),
        config=BatchConfig(control_fraction=args.control, seed=args.seed),
    )

    result = runner.run(now=corpus.now, customers=corpus.customers)
    m = result.metrics

    print("=" * 68)
    print("  RECOUPER -- batch result")
    print("=" * 68)
    print(f"\nCorpus            {corpus.summary()['total_records']} records "
          f"(seed {args.seed})")
    print(f"Cases extracted   {result.cases_total}")
    print(f"  by kind         {result.extraction['by_kind']}")
    print(f"  deduplicated    {result.extraction['skipped_orders_already_counted_as_payments']}"
          f" orders already counted as failed payments")
    print(f"\nArms              treated={m.n_treated}  control={m.n_control}")

    print("\n--- RECOVERY " + "-" * 55)
    print(f"Treated rate      {m.treated_rate:.1%}")
    print(f"Control rate      {m.control_rate:.1%}   <- self-recovery, no help from us")
    lift = m.lift
    sig = "significant" if lift.excludes_zero else "NOT significant (CI contains zero)"
    print(f"Lift              {lift.point:+.1%}  "
          f"[95% CI {lift.low:+.1%}, {lift.high:+.1%}]  {sig}")

    print("\n--- MONEY " + "-" * 58)
    print(f"Gross recovered   {_rupees(m.gross_recovered_paise)}"
          f"   <- INFLATED, includes self-recovery")
    print(f"Incremental       {_rupees(m.incremental_recovered_paise)}"
          f"   <- the honest number")
    if m.incremental_recovered_ci_paise:
        lo, hi = m.incremental_recovered_ci_paise
        print(f"  95% CI          [{_rupees(lo)}, {_rupees(hi)}]")
    if m.overstatement_factor:
        print(f"Overstatement     {m.overstatement_factor:.1f}x  "
              f"(reporting gross would overstate impact by this much)")

    print("\n--- COST OF INTERVENING " + "-" * 44)
    print(f"Contacts sent     {m.contacts_sent}")
    print(f"Retries attempted {m.retries_attempted}")
    print(f"Total cost        {_rupees(m.total_cost_paise)}")
    print(f"Wasted contacts   {m.wasted_contacts}  "
          f"(customers who would have paid anyway)")

    print("\n--- BOUNDS THAT BOUND " + "-" * 46)
    if result.denials:
        for rule, n in result.denials.items():
            print(f"  {rule:<28} {n:>5} blocked")
    else:
        print("  (no denials -- suspicious; check the policy engine is wired in)")
    print(f"Escalated         {m.escalated}")
    print(f"Exceptions        {m.excepted}")
    print(f"Dead letters      {len(result.dead_letters)}")

    if result.halted:
        print("\n--- BATCH HALTED " + "-" * 51)
        print(f"  {result.halt_reason}")
        print(f"  {len(result.dead_letters)} cases parked in the DLQ with context.")
        print("  Resume is a manual operation -- the breaker does not self-heal.")

    if result.planner_stats:
        print(f"\nPlanner           {result.planner_stats}")

    v = ledger.verify()
    print(f"\nAudit chain       {v.describe()}")
    print(f"Audit log         {ledger_path}")

    print("\n" + "=" * 68)
    print("  All recovery outcomes are SIMULATED under the model in")
    print("  recouper/outcomes/simulate.py. The contribution is the")
    print("  measurement framework, not the absolute rupee figure.")
    print("=" * 68)

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "seed": args.seed,
                    "corpus": corpus.summary(),
                    "extraction": result.extraction,
                    "metrics": m.to_dict(),
                    "denials": result.denials,
                    "dead_letters": result.dead_letters,
                    "halted": result.halted,
                    "halt_reason": result.halt_reason,
                    "simulation_parameters": model_parameters(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {args.json}")

    if args.html:
        from .report import write_report

        payload = {
            "seed": args.seed,
            "corpus": corpus.summary(),
            "extraction": result.extraction,
            "metrics": m.to_dict(),
            "denials": result.denials,
            "dead_letters": result.dead_letters,
            "halted": result.halted,
            "halt_reason": result.halt_reason,
        }
        print(f"Wrote {write_report(payload, args.html)}")
    return 0


def _silent_batch(seed: int, control: float):
    """Run one batch with no console output and return the result.

    The sensitivity analysis needs what the agent DID, not what it reported,
    so this keeps the traces and discards the printed metrics.
    """
    corpus = generate(seed=seed, now=DEFAULT_NOW)
    client = MockRazorpayClient.from_corpus(corpus, faults=FaultConfig())
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"audit_sensitivity_seed{seed}.jsonl"
    if path.exists():
        path.unlink()
    ledger = AuditLedger(path)
    executor = Executor(
        client=client, ledger=ledger,
        breaker=CircuitBreaker(threshold=0.20, window=50,
                               min_observations=10, min_failures=5),
        sleep=lambda s: None,
    )
    runner = BatchRunner(
        client=client, ledger=ledger, executor=executor,
        planner=DeterministicPlanner(),
        outcomes=OutcomeModel(seed=seed),
        config=BatchConfig(control_fraction=control, seed=seed),
    )
    return runner.run(now=corpus.now, customers=corpus.customers)


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def batch_spread(runs: int, control: float = 0.20) -> sens.BatchSpread:
    """Run the whole pipeline once per seed and collect the headline.

    Deliberately re-runs everything rather than replaying: the point is to
    include the corpus draw and the treatment randomisation, which replay
    holds fixed. Slow, and worth it -- this is the number that says whether
    a single run's headline means anything.
    """
    lifts: list[float] = []
    overstatements: list[float] = []
    significant = 0

    for seed in range(runs):
        result = _silent_batch(seed, control)
        m = result.metrics
        lifts.append(m.lift.point)
        significant += int(m.lift.excludes_zero)
        if m.overstatement_factor:
            overstatements.append(m.overstatement_factor)

    return sens.BatchSpread(
        lifts=lifts, overstatements=overstatements, significant=significant
    )


def cmd_sensitivity(args: argparse.Namespace) -> int:
    result = _silent_batch(args.seed, args.control)
    traces = result.traces
    n_t = sum(1 for t in traces if t.arm == "treated")
    n_c = sum(1 for t in traces if t.arm == "control")

    print("=" * 68)
    print("  DOES THE CONCLUSION SURVIVE THE ASSUMPTIONS?")
    print("=" * 68)
    print()
    print(f"One batch, replayed. {n_t} treated / {n_c} control. The agent's")
    print("behaviour is held fixed throughout -- only the simulator moves.")
    print()

    print("--- 1. HOW MUCH OF THE HEADLINE IS LUCK? ---------------------------")
    ss = sens.seed_stability(traces, draws=args.draws)
    print(f"Redrew outcomes under {ss.n_draws} seeds, parameters unchanged.")
    print()
    print(f"  Lift            mean {_pct(ss.mean_lift)}, sd {ss.sd_lift * 100:.1f}pp")
    print(f"  90% of draws    [{_pct(ss.percentile(ss.lifts, 0.05))}, "
          f"{_pct(ss.percentile(ss.lifts, 0.95))}]")
    print(f"  Positive in     {ss.positive_fraction * 100:.0f}% of draws")
    print(f"  SIGNIFICANT in  {ss.significant_fraction * 100:.0f}% of draws"
          "   <- this batch is underpowered")
    print()
    print("  A single run's headline is mostly a draw. Reporting one batch as")
    print("  a finding would be the same overclaiming this project exists to")
    print("  avoid, moved one layer down.")
    print()

    print("--- 2. WHICH ASSUMPTIONS ACTUALLY MATTER? --------------------------")
    points = sens.one_at_a_time(traces)
    inf = sens.influence(points)
    print("Scaled each group 0.5x-2.0x alone; range of mean lift induced:")
    print()
    for group, span in inf.items():
        bar = "#" * max(1, int(span * 200))
        print(f"  {group:<22} {span * 100:5.1f}pp  {bar}")
    print()
    print("  Costs move the lift by exactly nothing, which is the correct")
    print("  answer -- they never enter the recovery probability. A sweep")
    print("  showing otherwise would mean the harness was wired wrong.")
    print()

    print("--- 3. DOES IT HOLD ACROSS THE WHOLE SPACE? ------------------------")
    rb = sens.monte_carlo(traces, draws=args.mc_draws, low=args.low, high=args.high)
    print(f"Perturbed every group at once, log-uniform on "
          f"[{args.low}x, {args.high}x], {rb.n_draws} draws.")
    print()
    labels = {
        "gross_overstates_by_2x_or_more": "Gross overstates impact by >=2x",
        "lift_is_positive": "The intervention helps at all",
        "single_batch_lift_exceeds_5pp": "One batch shows a >5pp lift",
    }
    for name, label in labels.items():
        rate = rb.rate(name)
        defined = rb.defined.get(name, 0)
        if rate is None:
            print(f"  {label:<34} undefined in every draw")
            continue
        note = "" if defined == rb.n_draws else f"  (defined in {defined})"
        print(f"  {label:<34} holds in {rate * 100:5.1f}% of draws{note}")

    spread = None
    if args.full_seeds:
        print("--- 4. AND ACROSS WHOLE RUNS? --------------------------------------")
        spread = batch_spread(args.full_seeds, args.control)
        sd = spread.to_dict()
        print(f"Ran the entire pipeline {sd['n_runs']} times, fresh corpus and fresh")
        print("randomisation each time -- not a replay.")
        print()
        print(f"  Mean lift       {_pct(sd['mean_lift'])}, sd {sd['sd_lift'] * 100:.1f}pp")
        print(f"  Range           {_pct(sd['min_lift'])} to {_pct(sd['max_lift'])}")
        print(f"  Positive in     {sd['positive_runs']}/{sd['n_runs']} runs")
        print(f"  Significant in  {sd['significant_runs']}/{sd['n_runs']} runs")
        if sd["median_overstatement"]:
            print(f"  Median overstatement  {sd['median_overstatement']}x")
        print()

    print("=" * 68)
    print("  The overstatement finding is a property of the SYSTEM.")
    print("  The rupee figure is a property of the PARAMETERS.")
    print("=" * 68)

    if args.json:
        payload = {
            "batch": {"n_treated": n_t, "n_control": n_c, "seed": args.seed},
            "seed_stability": ss.to_dict(),
            "one_at_a_time": [p.to_dict() for p in points],
            "influence": {k: round(v, 5) for k, v in inf.items()},
            "robustness": rb.to_dict(),
            "batch_spread": spread.to_dict() if spread else None,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


def cmd_redteam(args: argparse.Namespace) -> int:
    from .redteam.corpus import CORPUS
    from .redteam.harness import run_corpus, taint_report

    report = run_corpus(CORPUS)
    d = report.to_dict()

    print("=" * 68)
    print("  PROMPT INJECTION -- CONTAINMENT REPORT")
    print("=" * 68)
    print()
    print("Threat model: the model is assumed FULLY COMPROMISED. It reads the")
    print("attacker's text and returns exactly the plan the attacker wanted,")
    print("every time, with a plausible rationale attached. We are not")
    print("measuring whether Claude resists these prompts -- that is a")
    print("property of the model. We are measuring what an attacker gains")
    print("when it does not resist at all.")
    print()

    print("--- CONTAINMENT " + "-" * 52)
    print(f"  Injection attempts        {d['total_attempts']:>4}")
    print(f"  Reached the planner       {d['total_attempts']:>4}   (all of them, by design)")
    print(f"  Stopped by schema         {d['stopped_by_schema']:>4}   "
          "invalid action -> whole plan rejected")
    print(f"  Stopped by policy         {d['stopped_by_policy']:>4}   "
          "legal action, forbidden target")
    print(f"  POLICY BYPASSES           {d['escapes']:>4}", end="")
    print("   <- the only number that may not move" if d["escapes"] == 0
          else "   <- FINDING, not a statistic")
    print()

    print("--- BY ATTACK FAMILY " + "-" * 47)
    print(f"  {'family':<22}{'n':>4}{'schema':>8}{'policy':>8}{'escaped':>9}")
    for family, row in d["by_family"].items():
        print(f"  {family:<22}{row['n']:>4}{row['schema']:>8}"
              f"{row['policy']:>8}{row['escaped']:>9}")
    print()

    print("--- BY ATTACKER OBJECTIVE " + "-" * 42)
    for objective, row in d["by_objective"].items():
        status = "contained" if row["escaped"] == 0 else f"ESCAPED x{row['escaped']}"
        print(f"  {objective:<20}{row['n']:>4} attempts   {status}")
    print()

    print("--- WHICH RULES DID THE WORK " + "-" * 39)
    for rule, n in d["denial_rules"].items():
        print(f"  {rule:<28}{n:>4} denials")
    print()

    print("--- WHERE ATTACKER TEXT ENDS UP " + "-" * 36)
    t = d["taint"]
    print("Blocking the action is not the whole story -- the text still flows.")
    print()
    print(f"  Into the message body           {t['reaches_message_body']:>3} / {t['n']}"
          "   body is template-derived")
    print(f"  Into an outbound customer field {t['reaches_outbound_customer_field']:>3} / {t['n']}"
          "   names are attacker-set")
    print(f"  Into the audit rationale        {t['reaches_audit_rationale']:>3} / {t['n']}"
          "   logged verbatim, by design")
    print()
    print("  The rationale figure is not a leak: a ledger that dropped hostile")
    print("  input would be a worse ledger. It is stated because anything that")
    print("  renders the audit log must treat it as untrusted text.")
    print()

    if args.verbose:
        print("--- EVERY ATTEMPT " + "-" * 50)
        for a in d["attempts"]:
            print(f"  {a['id']:<8} {a['objective']:<18} "
                  f"stopped at {a['stopped_at']:<18} {a['field']}")
        print()

    print("=" * 68)
    if d["escapes"] == 0:
        print("  0 bypasses. The bound is enforced by code the model")
        print("  cannot address, so a fully compromised model gains nothing.")
    else:
        print(f"  {d['escapes']} BYPASSES -- the containment claim does not hold.")
    print("=" * 68)

    if args.json:
        Path(args.json).write_text(json.dumps(d, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0 if d["escapes"] == 0 else 1


def cmd_verify(args: argparse.Namespace) -> int:
    ledger = AuditLedger(args.path)
    v = ledger.verify()
    print(v.describe())
    return 0 if v.ok else 1


def cmd_params(args: argparse.Namespace) -> int:
    print(json.dumps(model_parameters(), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="recouper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a recovery batch")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--control", type=float, default=0.20,
                   help="fraction held out as an untouched control arm")
    r.add_argument("--faults", action="store_true",
                   help="inject a gateway outage to demonstrate the breaker")
    r.add_argument("--fault-rate", type=float, default=0.90,
                   help="per-call gateway failure rate when --faults is set")
    r.add_argument("--healthy-calls", type=int, default=40,
                   help="calls to serve cleanly before faults begin")
    r.add_argument("--no-llm", action="store_true",
                   help="force the deterministic planner")
    r.add_argument("--json", help="write the full result to this path")
    r.add_argument("--html", help="write a static HTML report to this path")
    r.set_defaults(func=cmd_run)

    sn = sub.add_parser(
        "sensitivity",
        help="test whether the conclusions survive the simulator's assumptions",
    )
    sn.add_argument("--seed", type=int, default=42)
    sn.add_argument("--control", type=float, default=0.20)
    sn.add_argument("--draws", type=int, default=300,
                    help="outcome redraws for the seed-stability pass")
    sn.add_argument("--mc-draws", type=int, default=400,
                    help="joint parameter draws for the robustness pass")
    sn.add_argument("--low", type=float, default=0.5,
                    help="lower multiplier for the joint sweep")
    sn.add_argument("--high", type=float, default=2.0,
                    help="upper multiplier for the joint sweep")
    sn.add_argument("--full-seeds", type=int, default=0,
                    help="also re-run the whole pipeline this many times "
                         "(slow; includes corpus and assignment variation)")
    sn.add_argument("--json", help="write the full analysis to this path")
    sn.set_defaults(func=cmd_sensitivity)

    rt = sub.add_parser(
        "redteam",
        help="prompt-injection containment report against a compromised model",
    )
    rt.add_argument("--verbose", action="store_true",
                    help="list every attempt and where it was stopped")
    rt.add_argument("--json", help="write the full report to this path")
    rt.set_defaults(func=cmd_redteam)

    v = sub.add_parser("verify", help="verify an audit chain")
    v.add_argument("path")
    v.set_defaults(func=cmd_verify)

    p = sub.add_parser("params", help="print simulation parameters")
    p.set_defaults(func=cmd_params)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
