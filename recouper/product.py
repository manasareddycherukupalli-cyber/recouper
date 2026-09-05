"""Stateful orchestration for the local Recouper operator product.

The command-line runner is intentionally a one-shot batch demonstration. The
dashboard needs a slightly different lifecycle: prepare a batch, let a human
review the proposed plans, execute approved work, and checkpoint after every
action so a breaker can be resumed without replaying completed work.

This module keeps that product lifecycle separate from the CLI while reusing
the same provider protocol, planner, policy engine, executor, simulator, and
metrics implementation.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .agent.execute import BatchHalted, CircuitBreaker, Executor
from .agent.gemini import GeminiPlanner
from .agent.plan import DeterministicPlanner, LLMPlanner, Plan, Planner
from .audit.ledger import AuditLedger
from .data.generate import DEFAULT_NOW, generate
from .detect.score import Case, extract_cases
from .eval.metrics import BatchMetrics, CaseOutcome, compute
from .outcomes.simulate import OutcomeModel
from .policy.engine import PolicyEngine
from .policy.rules import (
    ActionType,
    CustomerState,
    Decision,
    DebtState,
    ProposedAction,
)
from .providers.mock import FaultConfig, MockRazorpayClient


FINAL_CASE_STATUSES = {
    "executed",
    "closed",
    "policy_blocked",
    "rejected",
    "escalated",
    "dead_lettered",
}


@dataclass
class ManagedCase:
    """A serialisable product view over one extracted recovery case."""

    case: Case
    arm: str
    plan: Optional[Plan] = None
    preview: list[dict] = field(default_factory=list)
    status: str = "awaiting_review"
    decision: Optional[str] = None
    decision_note: Optional[str] = None
    action_index: int = 0
    executed_actions: list[str] = field(default_factory=list)
    action_history: list[dict] = field(default_factory=list)
    contacts_sent: int = 0
    retries_attempted: int = 0
    previous_contacts: int = 0
    outcome: Optional[CaseOutcome] = None
    exception: Optional[dict] = None

    def to_dict(self) -> dict:
        case = self.case.to_dict()
        case.update(
            {
                "source_id": self.case.source_id,
                "hard_stops": {
                    "open_dispute": self.case.has_open_dispute,
                    "chargeback": self.case.has_chargeback,
                    "refunded": self.case.was_refunded,
                    "settled": self.case.is_settled,
                },
            }
        )
        return {
            **case,
            "arm": self.arm,
            "plan": self.plan.to_dict() if self.plan else None,
            "preview": self.preview,
            "status": self.status,
            "decision": self.decision,
            "decision_note": self.decision_note,
            "action_index": self.action_index,
            "executed_actions": list(self.executed_actions),
            "action_history": list(self.action_history),
            "contacts_sent": self.contacts_sent,
            "retries_attempted": self.retries_attempted,
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "exception": self.exception,
        }


@dataclass
class DashboardRun:
    run_id: str
    seed: int
    control_fraction: float
    original_faults: bool
    faults_enabled: bool
    no_llm: bool
    now: int
    created_at: float
    base_dir: Path
    ledger_path: Path
    corpus_summary: dict
    extraction: dict
    cases: list[ManagedCase]
    customers: dict[str, Any]
    client: MockRazorpayClient
    ledger: AuditLedger
    planner: Planner
    policy: PolicyEngine
    outcomes: OutcomeModel
    executor: Executor
    contacts: dict[str, dict[str, int]] = field(default_factory=dict)
    last_contact: dict[str, int] = field(default_factory=dict)
    metrics: Optional[BatchMetrics] = None
    status: str = "awaiting_approval"
    halt_reason: Optional[str] = None
    next_treated_index: int = 0
    actions_used: int = 0
    bulk_approval: bool = False

    # Running tallies kept in memory. Recomputing them from the ledger on
    # every checkpoint meant re-reading the whole JSONL file per action,
    # which is what made a 340-case batch take half a minute.
    denials: dict[str, int] = field(default_factory=dict)
    last_verification: Optional[dict] = None
    last_persist_at: float = 0.0

    @property
    def snapshot_path(self) -> Path:
        return self.base_dir / f"{self.run_id}.json"

    @property
    def treated_cases(self) -> list[ManagedCase]:
        return [c for c in self.cases if c.arm == "treated"]

    @property
    def control_cases(self) -> list[ManagedCase]:
        return [c for c in self.cases if c.arm == "control"]


class DashboardService:
    """Create and operate local, simulator-backed dashboard runs."""

    def __init__(self, base_dir: str | Path = "runs") -> None:
        self.base_dir = Path(base_dir)
        self._runs: dict[str, DashboardRun] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_run(
        self,
        *,
        seed: int = 42,
        control_fraction: float = 0.20,
        faults: bool = False,
        no_llm: bool = True,
    ) -> DashboardRun:
        if not 0 <= control_fraction < 1:
            raise ValueError("control_fraction must be at least 0 and below 1")

        self.base_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        corpus = generate(seed=seed, now=DEFAULT_NOW)
        fault_config = (
            FaultConfig(
                transient_failure_rate=0.90,
                healthy_calls=40,
                seed=5,
            )
            if faults
            else FaultConfig()
        )
        client = MockRazorpayClient.from_corpus(corpus, faults=fault_config)
        ledger_path = self.base_dir / f"{run_id}.jsonl"
        ledger = AuditLedger(ledger_path)
        planner: Planner = (
            DeterministicPlanner() if no_llm else LLMPlanner()
        )
        executor = Executor(
            client=client,
            ledger=ledger,
            breaker=CircuitBreaker(
                threshold=0.20,
                window=50,
                min_observations=10,
                min_failures=5,
            ),
            sleep=lambda _seconds: None,
        )
        report = extract_cases(
            payments=client.fetch_payments(count=10_000),
            orders=client.fetch_orders(count=10_000),
            invoices=client.fetch_invoices(count=10_000),
            customers=corpus.customers,
            now=corpus.now,
        )
        treated, control = self._assign_arms(report.cases, seed, control_fraction)
        ledger.append(
            actor="system",
            event="batch_started",
            seed=seed,
            control_fraction=control_fraction,
            supervised=True,
        )
        ledger.append(
            actor="system",
            event="arms_assigned",
            treated=len(treated),
            control=len(control),
        )

        session = DashboardRun(
            run_id=run_id,
            seed=seed,
            control_fraction=control_fraction,
            original_faults=faults,
            faults_enabled=faults,
            no_llm=no_llm,
            now=corpus.now,
            created_at=time.time(),
            base_dir=self.base_dir,
            ledger_path=ledger_path,
            corpus_summary=corpus.summary(),
            extraction=report.summary(),
            cases=[],
            customers={c.id: c for c in corpus.customers},
            client=client,
            ledger=ledger,
            planner=planner,
            policy=PolicyEngine(),
            outcomes=OutcomeModel(seed=seed),
            executor=executor,
        )

        # Control cases receive no plan. Keeping them visibly plan-less is a
        # useful product invariant and prevents accidental contamination.
        for case in control:
            session.cases.append(
                ManagedCase(case=case, arm="control", status="control")
            )

        for case in treated:
            plan = planner.plan(case)
            ledger.append(
                actor="llm" if plan.source == "llm" else "system",
                event="plan_created",
                case_id=case.case_id,
                reason=plan.rationale,
                result=plan.to_dict(),
            )
            managed = ManagedCase(
                case=case,
                arm="treated",
                plan=plan,
                preview=self._preview_plan(session, case, plan),
            )
            session.cases.append(managed)

        # The list is displayed in extracted priority order while controls
        # remain mixed into the same corpus for clear arm comparisons.
        session.cases.sort(key=lambda managed: -managed.case.expected_value_paise)
        self._runs[run_id] = session
        self._verify_session(session)
        self._persist(session, force=True)
        return session

    def list_runs(self) -> list[dict]:
        records: list[dict] = []
        self.base_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(
            self.base_dir.glob("run_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            records.append(
                {
                    "run_id": data.get("run_id", path.stem),
                    "status": data.get("status", "unknown"),
                    "seed": data.get("seed"),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "faults": data.get("original_faults", False),
                    "cases_total": data.get("extraction", {}).get("total_cases", 0),
                    "metrics": data.get("metrics"),
                }
            )
        return records

    def get_run(self, run_id: str) -> dict:
        session = self._runs.get(run_id)
        if session is not None:
            return self._public(session)
        path = self.base_dir / f"{run_id}.json"
        if not path.exists():
            raise KeyError(run_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return self._with_audit_snapshot(data)

    def get_cases(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        arm: Optional[str] = None,
        query: Optional[str] = None,
    ) -> list[dict]:
        data = self.get_run(run_id)
        cases = data.get("cases", [])
        query_lower = query.lower() if query else None
        out = []
        for case in cases:
            if status and case.get("status") != status:
                continue
            if arm and case.get("arm") != arm:
                continue
            if query_lower:
                haystack = json.dumps(case, sort_keys=True).lower()
                if query_lower not in haystack:
                    continue
            out.append(case)
        return out

    def get_case(self, run_id: str, case_id: str) -> dict:
        session = self._runs.get(run_id)
        if session is not None:
            managed = self._case(session, case_id)
            data = managed.to_dict()
            data["timeline"] = [
                record.to_dict() for record in session.ledger.case_timeline(case_id)
            ]
            return data
        for case in self.get_run(run_id).get("cases", []):
            if case.get("case_id") == case_id:
                return case
        raise KeyError(case_id)

    def approve_safe(self, run_id: str) -> DashboardRun:
        session = self._require_session(run_id)
        if session.status == "completed":
            return session
        if session.status == "halted":
            raise ValueError("run is halted; use resume before approving more work")
        session.bulk_approval = True
        self._ensure_control_outcomes(session)
        self._process_treated(session, approve_pending=True)
        self._persist(session, force=True)
        return session

    def decide_case(
        self,
        run_id: str,
        case_id: str,
        decision: str,
        note: str = "",
    ) -> DashboardRun:
        session = self._require_session(run_id)
        managed = self._case(session, case_id)
        if managed.arm != "treated":
            raise ValueError("control cases are observation-only and cannot be decided")
        if managed.status not in {"awaiting_review", "halted"}:
            raise ValueError(f"case is already {managed.status}")
        if decision not in {"approve", "reject", "escalate"}:
            raise ValueError("decision must be approve, reject, or escalate")

        self._ensure_control_outcomes(session)
        managed.decision = decision
        managed.decision_note = note.strip() or None
        session.ledger.append(
            actor="human",
            event=f"plan_{decision}d" if decision != "escalate" else "plan_escalated",
            case_id=case_id,
            decision=decision,
            reason=managed.decision_note or "operator decision from dashboard",
        )

        if decision == "reject":
            managed.status = "rejected"
            self._record_outcome(session, managed, excepted=True)
        elif decision == "escalate":
            managed.status = "escalated"
            self._record_outcome(session, managed, escalated=True)
        else:
            managed.status = "approved"
            try:
                self._execute_case(session, managed)
            except BatchHalted as exc:
                managed.status = "halted"
                session.status = "halted"
                session.halt_reason = str(exc)
                session.ledger.append(
                    actor="system", event="batch_halted", reason=str(exc)
                )

        self._refresh(session)
        self._persist(session, force=True)
        return session

    def replan_case(self, run_id: str, case_id: str) -> dict:
        """Re-plan one case with the Gemini planner, on demand.

        Deliberately one case rather than the batch. A free-tier key is rate
        limited to low tens of requests per minute, so re-planning 277 cases
        would take twenty minutes and read as a hang; and an operator only
        ever wants the model's reasoning for the case actually in front of
        them.

        Only permitted before the case has been decided. Re-planning after
        execution would rewrite the proposal that the ledger already records
        as having run, which would make the audit trail a lie.
        """
        session = self._require_session(run_id)
        managed = self._case(session, case_id)
        if managed.arm != "treated":
            raise ValueError("control cases are never planned; that is what makes them a control")
        if managed.status not in {"awaiting_review", "halted"}:
            raise ValueError(
                f"case is already {managed.status}; re-planning an executed case "
                "would contradict the audit record"
            )

        planner = GeminiPlanner()
        if not planner.available:
            raise ValueError("no GEMINI_API_KEY configured on the server")

        plan = planner.plan(managed.case)
        managed.plan = plan
        # Re-preview against live state, so the verdicts shown next to the new
        # proposal are the ones the gate would actually return for it.
        managed.preview = self._preview_plan(session, managed.case, plan)

        session.ledger.append(
            actor="llm" if plan.source == "gemini" else "system",
            event="plan_created",
            case_id=case_id,
            reason=plan.rationale,
            result=plan.to_dict(),
        )
        self._persist(session, force=True)
        return self.get_case(run_id, case_id)

    def resume(self, run_id: str) -> DashboardRun:
        session = self._require_session(run_id)
        if session.status != "halted":
            raise ValueError("only halted runs can be resumed")

        # The demo's fault mode represents an outage that an operator has
        # inspected and cleared. The same mock client is retained, including
        # its idempotency cache and any completed writes.
        session.client.faults = FaultConfig()
        session.faults_enabled = False
        session.executor.breaker.reset()
        session.status = "running"
        session.ledger.append(
            actor="human",
            event="batch_resumed",
            reason="operator confirmed the gateway is healthy",
            checkpoint=session.next_treated_index,
        )
        for managed in session.treated_cases:
            if managed.status == "halted":
                managed.status = "approved"
        self._process_treated(session, approve_pending=session.bulk_approval)
        self._persist(session, force=True)
        return session

    def verify(self, run_id: str) -> dict:
        session = self._runs.get(run_id)
        if session is not None:
            result = self._verify_session(session)
            self._persist(session, force=True)
            return result
        data = self.get_run(run_id)
        return data.get("audit", {})

    def audit(self, run_id: str, case_id: Optional[str] = None) -> list[dict]:
        """Return ledger records, optionally narrowed to one case."""
        session = self._runs.get(run_id)
        if session is not None:
            records = session.ledger.read_all()
        else:
            data = self.get_run(run_id)
            path = Path(data.get("ledger_path", ""))
            if not path.exists():
                raise KeyError(run_id)
            records = AuditLedger(path).read_all()
        return [
            record.to_dict()
            for record in records
            if case_id is None or record.case_id == case_id
        ]

    # ------------------------------------------------------------------
    # Preparation and execution internals
    # ------------------------------------------------------------------

    @staticmethod
    def _assign_arms(
        cases: list[Case], seed: int, control_fraction: float
    ) -> tuple[list[Case], list[Case]]:
        rng = random.Random(seed)
        treated, control = [], []
        for case in cases:
            (control if rng.random() < control_fraction else treated).append(case)
        return treated, control

    def _preview_plan(
        self, session: DashboardRun, case: Case, plan: Plan
    ) -> list[dict]:
        """Preview checks without mutating contact history or the ledger."""
        cid = case.customer_id or ""
        contact_counts = dict(session.contacts.get(cid, {}))
        last_contact = session.last_contact.get(cid)
        retries = 0
        rows = []
        for logical_attempt, action in enumerate(plan.actions, start=1):
            customer_state = self._customer_state(
                session,
                case,
                contacts=contact_counts,
                last_contact=last_contact,
            )
            proposed = self._proposed(
                session, case, customer_state, action, retries, plan.rationale
            )
            verdict = session.policy.gate(proposed)
            rows.append(
                {
                    "logical_attempt": logical_attempt,
                    "action": action.value,
                    "decision": verdict.decision.value,
                    "allowed": verdict.allowed,
                    "reason": verdict.reason,
                    "retryable_later": verdict.is_retryable_later,
                    "checks": [check.to_dict() for check in verdict.checks],
                }
            )
            if verdict.allowed:
                if action.contacts_customer:
                    contact_counts[case.case_id] = contact_counts.get(case.case_id, 0) + 1
                    last_contact = session.now
                if action.moves_money:
                    retries += 1
        return rows

    def _process_treated(
        self, session: DashboardRun, *, approve_pending: bool
    ) -> None:
        session.status = "running"
        treated = session.treated_cases
        for index in range(session.next_treated_index, len(treated)):
            managed = treated[index]
            if managed.status in FINAL_CASE_STATUSES:
                session.next_treated_index = index + 1
                continue
            if managed.status == "awaiting_review":
                if not approve_pending:
                    break
                managed.decision = "approve"
                managed.decision_note = "bulk approval of policy-gated plans"
                session.ledger.append(
                    actor="human",
                    event="plan_approved",
                    case_id=managed.case.case_id,
                    decision="approve",
                    reason=managed.decision_note,
                )
                managed.status = "approved"
            if managed.status not in {"approved", "halted"}:
                session.next_treated_index = index + 1
                continue
            if managed.status == "halted":
                managed.status = "approved"
            try:
                self._execute_case(session, managed)
            except BatchHalted as exc:
                managed.status = "halted"
                session.status = "halted"
                session.halt_reason = str(exc)
                session.next_treated_index = index
                session.ledger.append(
                    actor="system", event="batch_halted", reason=str(exc)
                )
                self._refresh(session)
                return
            session.next_treated_index = index + 1
            self._persist(session)

        self._refresh(session)

    def _execute_case(self, session: DashboardRun, managed: ManagedCase) -> None:
        if managed.plan is None:
            raise ValueError("treated case has no plan")
        case = managed.case
        customer = session.customers.get(case.customer_id or "")
        managed.previous_contacts = session.contacts.get(case.customer_id or "", {}).get(
            case.case_id, 0
        )

        while managed.action_index < len(managed.plan.actions):
            logical_attempt = managed.action_index + 1
            action = managed.plan.actions[managed.action_index]
            customer_state = self._customer_state(session, case)
            proposed = self._proposed(
                session,
                case,
                customer_state,
                action,
                managed.retries_attempted,
                managed.plan.rationale,
            )
            verdict = session.policy.gate(proposed)
            history = {
                "logical_attempt": logical_attempt,
                "action": action.value,
                "decision": verdict.decision.value,
                "allowed": verdict.allowed,
                "reason": verdict.reason,
                "retryable_later": verdict.is_retryable_later,
                "checks": [check.to_dict() for check in verdict.checks],
            }

            if not verdict.allowed:
                self._count_denials(session, verdict)
                session.ledger.append(
                    actor="policy",
                    event=(
                        "action_denied"
                        if verdict.decision is Decision.DENY
                        else "action_escalated"
                    ),
                    case_id=case.case_id,
                    action=action.value,
                    decision=verdict.decision.value,
                    reason=verdict.reason,
                    policy_checks=[check.to_dict() for check in verdict.checks],
                    retryable_later=verdict.is_retryable_later,
                )
                managed.action_history.append(history)
                if verdict.decision is Decision.ESCALATE:
                    managed.status = "escalated"
                managed.action_index += 1
                continue

            result = session.executor.execute(
                proposed,
                attempt_no=logical_attempt,
                payload=self._payload(case, customer),
            )
            session.actions_used += 1
            history["execution"] = {
                "ok": result.ok,
                "attempts": result.attempts,
                "response": result.response,
                "error": result.error,
                "dead_lettered": result.dead_lettered,
            }
            managed.action_history.append(history)
            if result.ok:
                managed.executed_actions.append(action.value)
                if action.contacts_customer:
                    managed.contacts_sent += 1
                    self._record_contact(session, case)
                if action.moves_money:
                    managed.retries_attempted += 1
            else:
                managed.exception = {
                    "case_id": case.case_id,
                    "class": case.case_class,
                    "reason": result.error or "action failed",
                }
            managed.action_index += 1

        if case.case_class == "unclassified" and managed.exception is None:
            managed.exception = {
                "case_id": case.case_id,
                "class": "unclassified",
                "reason": "failure reason not in the taxonomy; no automated action",
            }
        if managed.status not in {"escalated", "halted"}:
            if managed.exception and not managed.executed_actions:
                managed.status = "dead_lettered"
            elif managed.executed_actions and any(
                action == ActionType.CLOSE_NO_ACTION.value
                for action in managed.executed_actions
            ):
                managed.status = "closed"
            elif managed.executed_actions:
                managed.status = "executed"
            else:
                managed.status = "policy_blocked"
        self._record_outcome(
            session,
            managed,
            escalated=managed.status == "escalated",
            excepted=bool(managed.exception),
        )

    def _record_outcome(
        self,
        session: DashboardRun,
        managed: ManagedCase,
        *,
        escalated: bool = False,
        excepted: bool = False,
    ) -> None:
        if managed.outcome is not None:
            return
        case = managed.case
        actions = list(managed.executed_actions)
        model = OutcomeModel(seed=self._case_seed(session, managed))
        probability = model.recovery_probability(
            case_class=case.case_class,
            age_s=case.age_s,
            actions=actions,
            contacts_already_sent=managed.previous_contacts,
        )
        managed.outcome = CaseOutcome(
            case_id=case.case_id,
            case_class=case.case_class,
            arm=managed.arm,
            amount_paise=case.amount_paise,
            recovered=model.draw(probability),
            contacts_sent=managed.contacts_sent,
            retries_attempted=managed.retries_attempted,
            cost_paise=model.action_cost_paise(
                actions,
                llm_used=(managed.plan is not None and managed.plan.source == "llm"),
            ),
            escalated=escalated,
            excepted=excepted,
        )

    def _ensure_control_outcomes(self, session: DashboardRun) -> None:
        for managed in session.control_cases:
            if managed.outcome is None:
                self._record_outcome(session, managed)

    def _refresh(self, session: DashboardRun) -> None:
        self._ensure_control_outcomes(session)
        outcomes = [managed.outcome for managed in session.cases if managed.outcome]
        session.metrics = compute(outcomes) if outcomes else None
        if session.status != "halted":
            treated_done = all(
                managed.status in FINAL_CASE_STATUSES
                for managed in session.treated_cases
            )
            session.status = "completed" if treated_done else "awaiting_approval"
        if session.status in {"completed", "halted"}:
            self._verify_session(session)

    def _proposed(
        self,
        session: DashboardRun,
        case: Case,
        customer: CustomerState,
        action: ActionType,
        retries: int,
        rationale: str = "",
    ) -> ProposedAction:
        classification = case.classification
        debt = DebtState(
            debt_id=case.case_id,
            customer_id=case.customer_id or "",
            amount=case.amount_paise,
            retries_attempted=retries,
            max_retries_allowed=classification.max_retries if classification else 0,
            class_allows_contact=(
                classification.allows_contact if classification else True
            ),
            class_allows_silent_retry=(
                classification.can_retry_silently if classification else False
            ),
            has_open_dispute=case.has_open_dispute,
            has_chargeback=case.has_chargeback,
            was_refunded=case.was_refunded,
            is_settled=case.is_settled,
        )
        return ProposedAction(
            action=action,
            debt=debt,
            customer=customer,
            now_ts=session.now,
            rationale=rationale,
        )

    def _customer_state(
        self,
        session: DashboardRun,
        case: Case,
        *,
        contacts: Optional[dict[str, int]] = None,
        last_contact: Optional[int] = None,
    ) -> CustomerState:
        cid = case.customer_id or ""
        customer = session.customers.get(cid)
        if not cid or customer is None:
            return CustomerState(customer_id=cid, known=False)
        history = (
            contacts
            if contacts is not None
            else getattr(session, "contacts", {}).get(cid, {})
        )
        previous = (
            last_contact
            if last_contact is not None
            else getattr(session, "last_contact", {}).get(cid)
        )
        return CustomerState(
            customer_id=cid,
            do_not_contact=bool(customer.notes.get("do_not_contact", False)),
            contacts_by_debt=dict(history),
            last_contact_ts=previous,
            known=True,
        )

    @staticmethod
    def _payload(case: Case, customer) -> dict:
        return {
            "currency": "INR",
            "customer": (
                {
                    "name": customer.name,
                    "email": customer.email,
                    "contact": customer.contact,
                }
                if customer
                else {}
            ),
            "to": customer.email if customer else "",
            "channel": "email",
            "subject": "Your payment did not go through",
            "body": (
                f"We could not process Rs.{case.amount_paise / 100:,.0f}. "
                "You can complete it at your convenience."
            ),
            "description": f"Recovery for {case.case_id}",
        }

    @staticmethod
    def _case_seed(session: DashboardRun, managed: ManagedCase) -> int:
        raw = f"{session.seed}:{managed.arm}:{managed.case.case_id}"
        return int(hashlib.sha256(raw.encode()).hexdigest()[:8], 16)

    @staticmethod
    def _record_contact(session: DashboardRun, case: Case) -> None:
        cid = case.customer_id or ""
        session.contacts.setdefault(cid, {})
        session.contacts[cid][case.case_id] = (
            session.contacts[cid].get(case.case_id, 0) + 1
        )
        session.last_contact[cid] = session.now

    def _case(self, session: DashboardRun, case_id: str) -> ManagedCase:
        for managed in session.cases:
            if managed.case.case_id == case_id:
                return managed
        raise KeyError(case_id)

    @staticmethod
    def _count_denials(session: DashboardRun, verdict) -> None:
        """Tally which rules blocked something, as the block happens."""
        for check in verdict.checks:
            if check.decision in (Decision.DENY, Decision.ESCALATE):
                session.denials[check.rule_id] = (
                    session.denials.get(check.rule_id, 0) + 1
                )

    @staticmethod
    def _verify_session(session: DashboardRun) -> dict:
        """Walk the hash chain and cache the result on the session."""
        result = session.ledger.verify()
        session.last_verification = {
            "ok": result.ok,
            "records_checked": result.records_checked,
            "broken_at_seq": result.broken_at_seq,
            "problem": result.problem,
            "description": result.describe(),
        }
        return session.last_verification

    def _require_session(self, run_id: str) -> DashboardRun:
        session = self._runs.get(run_id)
        if session is None:
            raise KeyError(run_id)
        return session

    # ------------------------------------------------------------------
    # Persistence / API shapes
    # ------------------------------------------------------------------

    #: Minimum wall-clock gap between snapshot writes during a long batch.
    #: Case state is checkpointed in memory after every action; this only
    #: bounds how often that checkpoint is flushed to disk.
    PERSIST_INTERVAL_S = 0.5

    def _persist(self, session: DashboardRun, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - session.last_persist_at < self.PERSIST_INTERVAL_S:
            return
        session.last_persist_at = now
        payload = self._snapshot(session)
        tmp = session.snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(session.snapshot_path)

    def _snapshot(self, session: DashboardRun) -> dict:
        breaker = session.executor.breaker
        return {
            "run_id": session.run_id,
            "seed": session.seed,
            "control_fraction": session.control_fraction,
            "original_faults": session.original_faults,
            "faults_enabled": session.faults_enabled,
            "no_llm": session.no_llm,
            "now": session.now,
            "created_at": session.created_at,
            "updated_at": time.time(),
            "status": session.status,
            "halt_reason": session.halt_reason,
            "corpus": session.corpus_summary,
            "extraction": session.extraction,
            "metrics": session.metrics.to_dict() if session.metrics else None,
            "denials": dict(
                sorted(session.denials.items(), key=lambda kv: -kv[1])
            ),
            "dead_letters": [d.to_dict() for d in session.executor.dead_letters],
            "exceptions": [
                managed.exception for managed in session.cases if managed.exception
            ],
            "planner_stats": dict(getattr(session.planner, "stats", {})),
            "actions_used": session.actions_used,
            "next_treated_index": session.next_treated_index,
            "bulk_approval": session.bulk_approval,
            "contacts": session.contacts,
            "last_contact": session.last_contact,
            "breaker": {
                "state": breaker.state.value,
                "failure_rate": round(breaker.failure_rate, 4),
                "trip_reason": breaker.trip_reason,
            },
            "ledger_path": str(session.ledger_path),
            "audit": session.last_verification or {
                "ok": None,
                "records_checked": None,
                "description": "chain not verified since the last append",
            },
            "cases": [managed.to_dict() for managed in session.cases],
        }

    def _public(self, session: DashboardRun) -> dict:
        data = self._snapshot(session)
        treated_total = len(session.treated_cases)
        resolved = sum(
            1 for managed in session.treated_cases if managed.status in FINAL_CASE_STATUSES
        )
        data["progress"] = {
            "treated_total": treated_total,
            "treated_resolved": resolved,
            "awaiting_review": sum(
                1
                for managed in session.treated_cases
                if managed.status == "awaiting_review"
            ),
        }
        return data

    def _with_audit_snapshot(self, data: dict) -> dict:
        path = Path(data.get("ledger_path", ""))
        if path.exists():
            verification = AuditLedger(path).verify()
            data["audit"] = {
                "ok": verification.ok,
                "records_checked": verification.records_checked,
                "broken_at_seq": verification.broken_at_seq,
                "problem": verification.problem,
                "description": verification.describe(),
            }
        return data
