"""Batch orchestration: the end-to-end recovery run.

Pipeline per batch:

    extract cases
      -> randomise into treated / control arms
      -> for each TREATED case:
           plan -> gate each action -> execute allowed ones
      -> for each CONTROL case:
           observe only, never touch
      -> simulate outcomes for both arms
      -> compute honest metrics

The control arm is the reason this file is structured the way it is. It must
be genuinely untouched -- no contact, no retry, not even a plan -- or the
comparison is contaminated and every number downstream is worthless. The
assignment happens once, before any work, and is seeded so a run is
reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from .agent.execute import BatchHalted, Executor
from .agent.plan import DeterministicPlanner, LLMPlanner, Planner
from .audit.ledger import AuditLedger
from .detect.score import Case, extract_cases
from .eval.metrics import BatchMetrics, compute
from .eval.replay import CaseTrace, draw_outcomes
from .outcomes.simulate import OutcomeModel
from .policy.engine import PolicyEngine
from .policy.rules import ActionType, CustomerState, DebtState, Decision, ProposedAction
from .providers.protocol import RazorpayClient

_DAY = 86_400


@dataclass
class BatchConfig:
    control_fraction: float = 0.20
    seed: int = 1234
    max_actions: int = 2000
    """Hard budget on actions per run, so a bug cannot fan out across the
    whole customer base before anyone notices."""


@dataclass
class BatchResult:
    metrics: BatchMetrics
    cases_total: int
    extraction: dict
    denials: dict
    dead_letters: list
    halted: bool = False
    halt_reason: Optional[str] = None
    planner_stats: dict = field(default_factory=dict)
    exceptions: list = field(default_factory=list)
    traces: list = field(default_factory=list)
    """What the agent did, before any outcome was drawn. Retained so the
    sensitivity analysis can re-observe this exact batch under different
    simulator assumptions -- see eval/replay.py."""


class BatchRunner:
    def __init__(
        self,
        *,
        client: RazorpayClient,
        ledger: AuditLedger,
        executor: Executor,
        planner: Optional[Planner] = None,
        policy: Optional[PolicyEngine] = None,
        outcomes: Optional[OutcomeModel] = None,
        config: Optional[BatchConfig] = None,
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.executor = executor
        self.planner = planner or DeterministicPlanner()
        self.policy = policy or PolicyEngine()
        self.outcomes = outcomes or OutcomeModel()
        self.config = config or BatchConfig()

        # Our own record of how we have treated each customer. Sourced here
        # rather than from the provider because these are facts about our
        # behaviour, not theirs.
        self._contacts: dict[str, dict[str, int]] = {}
        self._last_contact: dict[str, int] = {}

    def run(self, *, now: int, customers: list) -> BatchResult:
        self.ledger.append(
            actor="system", event="batch_started",
            seed=self.config.seed, control_fraction=self.config.control_fraction,
        )

        report = extract_cases(
            payments=self.client.fetch_payments(count=10_000),
            orders=self.client.fetch_orders(count=10_000),
            invoices=self.client.fetch_invoices(count=10_000),
            customers=customers,
            now=now,
        )
        by_customer = {c.id: c for c in customers}

        treated, control = self._assign_arms(report.cases)
        self.ledger.append(
            actor="system", event="arms_assigned",
            treated=len(treated), control=len(control),
        )

        halted, halt_reason = False, None
        traces: list[CaseTrace] = []
        exceptions: list[dict] = []
        actions_used = 0

        # --- treated arm ---------------------------------------------------
        for case in treated:
            if actions_used >= self.config.max_actions:
                self.ledger.append(
                    actor="system", event="batch_budget_exhausted",
                    reason=f"reached the {self.config.max_actions}-action cap",
                )
                break

            try:
                trace, used, exc = self._work_case(
                    case, by_customer.get(case.customer_id or ""), now
                )
            except BatchHalted as exc_halt:
                halted, halt_reason = True, str(exc_halt)
                self.ledger.append(
                    actor="system", event="batch_halted", reason=str(exc_halt),
                )
                break

            actions_used += used
            traces.append(trace)
            if exc:
                exceptions.append(exc)

        # --- control arm: observed only, never touched ---------------------
        # No plan, no gate, no execution -- only a trace saying we left it
        # alone. Any code path here that touched the case would contaminate
        # the comparison the whole project rests on.
        for case in control:
            traces.append(
                CaseTrace(
                    case_id=case.case_id,
                    case_class=case.case_class,
                    arm="control",
                    amount_paise=case.amount_paise,
                    age_s=case.age_s,
                )
            )

        # Outcomes are drawn only now, in one place, for both arms at once.
        # Drawing inside the treated loop would have given the two arms
        # different positions in the RNG stream -- a subtle way to bias a
        # comparison that is supposed to differ only by treatment.
        outcomes = draw_outcomes(
            traces, params=self.outcomes.params, seed=self.outcomes.seed
        )
        metrics = compute(outcomes)
        self.ledger.append(
            actor="system", event="batch_completed",
            result={
                "cases": len(outcomes),
                "incremental_paise": metrics.incremental_recovered_paise,
                "gross_paise": metrics.gross_recovered_paise,
            },
        )

        planner_stats = getattr(self.planner, "stats", {})
        return BatchResult(
            metrics=metrics,
            cases_total=len(report.cases),
            extraction=report.summary(),
            denials=self.ledger.denial_counts(),
            dead_letters=[d.to_dict() for d in self.executor.dead_letters],
            halted=halted,
            halt_reason=halt_reason,
            planner_stats=dict(planner_stats),
            exceptions=exceptions,
            traces=traces,
        )

    # --- internals ---------------------------------------------------------

    def _assign_arms(self, cases: list[Case]) -> tuple[list[Case], list[Case]]:
        """Seeded random assignment.

        Random rather than, say, alternating by index: the case list is
        sorted by expected value, so any systematic rule would put
        higher-value cases disproportionately into one arm and bias the
        comparison from the outset.
        """
        rng = random.Random(self.config.seed)
        treated, control = [], []
        for case in cases:
            (control if rng.random() < self.config.control_fraction else treated).append(case)
        return treated, control

    def _work_case(
        self, case: Case, customer, now: int
    ) -> tuple[CaseTrace, int, Optional[dict]]:
        plan = self.planner.plan(case)
        self.ledger.append(
            actor="llm" if plan.source == "llm" else "system",
            event="plan_created", case_id=case.case_id,
            reason=plan.rationale, result=plan.to_dict(),
        )

        cust_state = self._customer_state(case, customer)
        executed: list[str] = []
        escalated = False
        excepted = False
        exception_detail = None
        actions_used = 0
        retries = 0
        contacts = 0

        for logical_attempt, action in enumerate(plan.actions, start=1):
            debt = DebtState(
                debt_id=case.case_id,
                customer_id=case.customer_id or "",
                amount=case.amount_paise,
                retries_attempted=retries,
                max_retries_allowed=(
                    case.classification.max_retries if case.classification else 0
                ),
                class_allows_contact=(
                    case.classification.allows_contact if case.classification else True
                ),
                class_allows_silent_retry=(
                    case.classification.can_retry_silently
                    if case.classification else False
                ),
                has_open_dispute=case.has_open_dispute,
                has_chargeback=case.has_chargeback,
                was_refunded=case.was_refunded,
                is_settled=case.is_settled,
            )
            proposed = ProposedAction(
                action=action, debt=debt, customer=cust_state,
                now_ts=now, rationale=plan.rationale,
            )
            verdict = self.policy.gate(proposed)

            if not verdict.allowed:
                # Denials are logged with the full check list -- this is the
                # evidence that the bounds bind.
                self.ledger.append(
                    actor="policy",
                    event="action_denied" if verdict.decision is Decision.DENY
                    else "action_escalated",
                    case_id=case.case_id, action=action.value,
                    decision=verdict.decision.value, reason=verdict.reason,
                    policy_checks=[c.to_dict() for c in verdict.checks],
                    retryable_later=verdict.is_retryable_later,
                )
                if verdict.decision is Decision.ESCALATE:
                    escalated = True
                continue

            if action is ActionType.ESCALATE_TO_HUMAN:
                escalated = True
            if action is ActionType.CLOSE_NO_ACTION:
                excepted = True
                exception_detail = {
                    "case_id": case.case_id,
                    "class": case.case_class,
                    "reason": plan.rationale[:200],
                }

            result = self.executor.execute(
                proposed,
                attempt_no=logical_attempt,
                payload=self._payload(case, customer),
            )
            actions_used += 1

            if result.ok:
                executed.append(action.value)
                if action.contacts_customer:
                    contacts += 1
                    self._record_contact(case, now)
                if action.moves_money:
                    retries += 1
            else:
                excepted = True
                exception_detail = {
                    "case_id": case.case_id,
                    "class": case.case_class,
                    "reason": result.error or "action failed",
                }

        if case.case_class == "unclassified":
            excepted = True
            exception_detail = exception_detail or {
                "case_id": case.case_id,
                "class": "unclassified",
                "reason": "failure reason not in the taxonomy; no automated action",
            }

        prior = self._contacts.get(case.customer_id or "", {}).get(case.case_id, 0)

        # What we did, recorded without saying whether it worked. The draw
        # happens once for the whole batch in run(), from this trace.
        trace = CaseTrace(
            case_id=case.case_id,
            case_class=case.case_class,
            arm="treated",
            amount_paise=case.amount_paise,
            age_s=case.age_s,
            executed=executed,
            contacts_already_sent=max(0, prior - contacts),
            contacts_sent=contacts,
            retries_attempted=retries,
            llm_used=(plan.source == "llm"),
            escalated=escalated,
            excepted=excepted,
        )
        return trace, actions_used, exception_detail

    def _customer_state(self, case: Case, customer) -> CustomerState:
        cid = case.customer_id or ""
        if not cid or customer is None:
            # Deny-by-default: an unknown customer cannot be contacted.
            return CustomerState(customer_id=cid, known=False)
        return CustomerState(
            customer_id=cid,
            do_not_contact=bool(customer.notes.get("do_not_contact", False)),
            contacts_by_debt=dict(self._contacts.get(cid, {})),
            last_contact_ts=self._last_contact.get(cid),
            known=True,
        )

    def _record_contact(self, case: Case, now: int) -> None:
        cid = case.customer_id or ""
        self._contacts.setdefault(cid, {})
        self._contacts[cid][case.case_id] = self._contacts[cid].get(case.case_id, 0) + 1
        self._last_contact[cid] = now

    @staticmethod
    def _payload(case: Case, customer) -> dict:
        return {
            "currency": "INR",
            "customer": (
                {"name": customer.name, "email": customer.email,
                 "contact": customer.contact}
                if customer else {}
            ),
            "to": customer.email if customer else "",
            "channel": "email",
            "subject": "Your payment did not go through",
            "body": (
                f"We could not process Rs.{case.amount_paise / 100:,.0f}. "
                f"You can complete it at your convenience."
            ),
            "description": f"Recovery for {case.case_id}",
        }
