"""Command-line entry point.

    python -m recouper.cli run            # full batch, honest metrics
    python -m recouper.cli run --faults   # gateway outage + breaker demo
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

    v = sub.add_parser("verify", help="verify an audit chain")
    v.add_argument("path")
    v.set_defaults(func=cmd_verify)

    p = sub.add_parser("params", help="print simulation parameters")
    p.set_defaults(func=cmd_params)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
