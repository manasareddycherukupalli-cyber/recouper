"""A second provider must not buy a second set of rules.

The point of `GeminiPlanner` is not that Gemini plans well. It is that the
bounds are a property of this codebase rather than of a vendor: the same
closed enum, the same reject-don't-repair validation, the same deterministic
fallback, and the same policy gate downstream.

So these tests deliberately do not call the API. They drive the planner with
stubbed transport and assert that a hostile, malformed, or unavailable
provider produces exactly the same containment as the Anthropic path.
"""

from __future__ import annotations

import json

import pytest

from recouper.agent.gemini import GeminiPlanner
from recouper.agent.plan import DeterministicPlanner, LEGAL_ACTIONS
from recouper.detect.classify import Classification, Grounding, RecoveryClass
from recouper.detect.score import Case
from recouper.policy.engine import PolicyEngine
from recouper.policy.rules import (
    ActionType,
    CustomerState,
    DebtState,
    Decision,
    ProposedAction,
)


def make_case(cls=RecoveryClass.DEFERRED, retry=True, amount=9_349_00) -> Case:
    return Case(
        case_id="pay_gemini_test",
        kind="failed_payment",
        case_class=cls.value,
        customer_id="cust_1",
        amount_paise=amount,
        age_s=8 * 86_400,
        classification=Classification(
            payment_id="pay_gemini_test",
            error_reason="insufficient_funds",
            recovery_class=cls,
            grounding=Grounding.GROUNDED,
            rationale="constructed for tests",
            max_retries=2,
            retry_delays_s=(),
            allows_contact=True,
            can_retry_silently=retry,
        ),
    )


def planner_returning(text: str) -> GeminiPlanner:
    """A planner whose transport returns exactly `text`."""
    p = GeminiPlanner(api_key="test-key-not-real")
    p._call = lambda case, allowed: text  # noqa: SLF001 -- the substitution is the test
    return p


def planner_raising(exc: Exception) -> GeminiPlanner:
    p = GeminiPlanner(api_key="test-key-not-real")

    def boom(case, allowed):
        raise exc

    p._call = boom  # noqa: SLF001
    return p


# --- the happy path ------------------------------------------------------


def test_a_valid_plan_is_accepted_and_labelled_gemini():
    p = planner_returning(
        json.dumps({"actions": ["retry_payment"], "rationale": "mandate on file"})
    )
    plan = p.plan(make_case())
    assert plan.actions == [ActionType.RETRY_PAYMENT]
    assert plan.source == "gemini"
    assert p.stats["ok"] == 1


def test_markdown_fenced_json_is_still_read():
    """Some Gemini models wrap JSON in a fence even when told not to."""
    p = planner_returning(
        '```json\n{"actions": ["send_reminder"], "rationale": "nudge"}\n```'
    )
    plan = p.plan(make_case())
    assert plan.actions == [ActionType.SEND_REMINDER]
    assert plan.source == "gemini"


# --- containment: the same bounds as the Anthropic path ------------------


def test_an_invented_action_invalidates_the_whole_plan():
    """Not "drop the bad one" -- the entire plan is rejected.

    Dropping it would execute a plan the model never proposed: a different
    plan, chosen by the error handler.
    """
    p = planner_returning(
        json.dumps(
            {"actions": ["retry_payment", "charge_saved_card"], "rationale": "ok"}
        )
    )
    plan = p.plan(make_case())
    assert plan.source == "gemini_fallback"
    assert ActionType.RETRY_PAYMENT in plan.actions or plan.actions
    assert all(a in set(ActionType) for a in plan.actions)
    assert p.stats["invalid"] == 1


def test_an_action_outside_this_class_is_rejected():
    """Legal enum member, illegal for this recovery class."""
    case = make_case(cls=RecoveryClass.TERMINAL, retry=False)
    p = planner_returning(
        json.dumps({"actions": ["send_reminder"], "rationale": "please pay"})
    )
    plan = p.plan(case)
    assert plan.source == "gemini_fallback"
    assert ActionType.SEND_REMINDER not in plan.actions


def test_a_retry_is_not_offered_when_no_mandate_is_held():
    case = make_case(retry=False)
    allowed = GeminiPlanner._allowed_for(case)
    assert ActionType.RETRY_PAYMENT not in allowed


def test_every_class_the_planner_can_see_is_a_subset_of_the_legal_table():
    for cls in RecoveryClass:
        case = make_case(cls=cls)
        allowed = set(GeminiPlanner._allowed_for(case))
        legal = set(LEGAL_ACTIONS.get(cls.value, [ActionType.ESCALATE_TO_HUMAN]))
        assert allowed <= legal


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "{}",
        json.dumps({"actions": [], "rationale": "empty"}),
        json.dumps({"actions": "retry_payment", "rationale": "not a list"}),
        json.dumps({"rationale": "no actions key"}),
        json.dumps({"actions": [123], "rationale": "not strings"}),
        '{"actions": ["retry_payment"], "rationale": "unterminated',
    ],
)
def test_malformed_output_falls_back_rather_than_guessing(payload):
    p = planner_returning(payload)
    plan = p.plan(make_case())
    assert plan.source == "gemini_fallback"
    assert plan.actions, "a fallback must still produce a usable plan"


# --- availability --------------------------------------------------------


def test_no_key_means_fallback_not_a_crash():
    p = GeminiPlanner(api_key="")
    p._key = ""  # noqa: SLF001 -- ignore any ambient GEMINI_API_KEY
    plan = p.plan(make_case())
    assert plan.source == "gemini_fallback"
    assert not p.available


def test_a_network_failure_falls_back():
    p = planner_raising(TimeoutError("read timed out"))
    plan = p.plan(make_case())
    assert plan.source == "gemini_fallback"
    assert "timed out" in plan.rationale
    assert p.stats["fallback"] == 1


def test_a_rate_limit_falls_back_rather_than_taking_the_batch_down():
    """A free tier will rate limit. Recovery work outranks optimal planning."""
    p = planner_raising(RuntimeError("HTTP 429 rate limit exceeded"))
    plan = p.plan(make_case())
    assert plan.source == "gemini_fallback"
    assert plan.actions


def test_the_fallback_plan_matches_the_deterministic_planner():
    case = make_case()
    p = planner_raising(RuntimeError("down"))
    assert p.plan(case).actions == DeterministicPlanner().plan(case).actions


# --- the property that matters: the gate does not care who proposed ------


def test_a_gemini_plan_is_gated_exactly_like_any_other():
    """Same proposal, same verdict, whoever authored it.

    A do-not-contact customer is refused whether the plan came from Gemini,
    from Claude, or from the lookup table. If this ever diverges, the
    provider has become part of the security boundary.
    """
    case = make_case()
    p = planner_returning(
        json.dumps({"actions": ["send_reminder"], "rationale": "worth a nudge"})
    )
    plan = p.plan(case)
    assert plan.source == "gemini"

    engine = PolicyEngine()
    verdict = engine.gate(
        ProposedAction(
            action=plan.actions[0],
            debt=DebtState(
                debt_id=case.case_id,
                customer_id="cust_1",
                amount=case.amount_paise,
                max_retries_allowed=2,
                class_allows_contact=True,
                class_allows_silent_retry=True,
            ),
            customer=CustomerState(customer_id="cust_1", do_not_contact=True),
            now_ts=1_767_252_600,
            rationale=plan.rationale,
        )
    )
    assert verdict.decision is Decision.DENY
    assert "do_not_contact" in verdict.blocking_rules


def test_the_planner_cannot_widen_its_own_allowed_set():
    """Asking for everything returns nothing extra."""
    case = make_case(cls=RecoveryClass.INSTRUMENT_DEAD, retry=False)
    p = planner_returning(
        json.dumps(
            {
                "actions": [a.value for a in ActionType],
                "rationale": "all of them please",
            }
        )
    )
    plan = p.plan(case)
    assert plan.source == "gemini_fallback"
    assert ActionType.RETRY_PAYMENT not in plan.actions
