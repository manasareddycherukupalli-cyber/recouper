"""A second planner, on a second provider.

The point of this file is not that Gemini is better than Claude at choosing a
recovery plan. It is that **the safety properties do not depend on which model
proposes.**

`GeminiPlanner` implements the same `Planner` protocol as `LLMPlanner` and
`DeterministicPlanner`, and reuses `LLMPlanner._parse` verbatim -- so the
closed `ActionType` enum, the reject-don't-repair validation, and the
deterministic fallback are literally the same code path. Downstream, the
policy engine gates every proposed action without knowing or caring where it
came from. Swapping the provider changes the wording of a rationale and
nothing about what the system is permitted to do.

That is worth being able to demonstrate rather than assert. The red-team
harness already tests containment against a *fully compromised* model, which
covers any provider by construction; this file is the same claim from the
other direction, with a real second vendor.

Two deliberate choices:

* **No new dependency.** This calls the REST endpoint through `urllib` from
  the standard library rather than pulling in a vendor SDK. A recovery agent
  that gains a transitive dependency tree per provider is a worse system, and
  the deployment needs nothing but an environment variable.

* **One case at a time.** Free tiers are rate-limited to low tens of requests
  per minute, so planning a 277-case batch through one would take twenty
  minutes and look broken. The dashboard calls this for a single case on
  demand, which fits the limit comfortably and is a better interaction than
  silently planning hundreds of cases nobody reads.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from ..detect.score import Case
from ..policy.rules import ActionType
from .plan import LEGAL_ACTIONS, SYSTEM_PROMPT, DeterministicPlanner, LLMPlanner, Plan

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# Chosen by measurement, not by version number. The flash-lite line answers in
# under two seconds with clean JSON; the larger flash models spend several
# hundred thinking tokens first and take upwards of thirty seconds, which is
# unusable behind an interactive button.
DEFAULT_MODEL = "gemini-flash-lite-latest"


@dataclass
class GeminiPlanner:
    """Gemini-backed planner with the same bounds and the same fallback."""

    model: str = DEFAULT_MODEL
    api_key: Optional[str] = None
    timeout_s: float = 20.0
    max_output_tokens: int = 400
    fallback: DeterministicPlanner = field(default_factory=DeterministicPlanner)

    stats: dict = field(
        default_factory=lambda: {"ok": 0, "fallback": 0, "invalid": 0}
    )

    def __post_init__(self) -> None:
        self._key = self.api_key or os.environ.get("GEMINI_API_KEY") or ""

    @property
    def available(self) -> bool:
        return bool(self._key)

    # --- the Planner protocol ------------------------------------------

    def plan(self, case: Case) -> Plan:
        if not self._key:
            return self._fall_back(case, "no GEMINI_API_KEY set")

        allowed = self._allowed_for(case)
        try:
            raw = self._call(case, allowed)
        except Exception as exc:  # network, timeout, rate limit, bad status
            self.stats["fallback"] += 1
            return self._fall_back(case, f"Gemini call failed ({exc})")

        # Reuse the Anthropic planner's validator rather than writing a second
        # one. Two parsers would be two chances to disagree about what counts
        # as a legal plan, and this is the layer where invented actions die.
        parsed = LLMPlanner._parse(raw, allowed)
        if parsed is None:
            self.stats["invalid"] += 1
            return self._fall_back(case, "Gemini output rejected by schema")

        actions, rationale = parsed
        self.stats["ok"] += 1
        return Plan(case.case_id, actions, rationale, "gemini")

    # --- internals ------------------------------------------------------

    @staticmethod
    def _allowed_for(case: Case) -> list[ActionType]:
        allowed = list(
            LEGAL_ACTIONS.get(case.case_class, [ActionType.ESCALATE_TO_HUMAN])
        )
        # Mirrors the deterministic planner: a retry is only conceivable when
        # the classification says we hold something reusable to charge.
        if case.classification and not case.classification.can_retry_silently:
            allowed = [a for a in allowed if a is not ActionType.RETRY_PAYMENT]
        return allowed

    def _fall_back(self, case: Case, why: str) -> Plan:
        p = self.fallback.plan(case)
        p.source = "gemini_fallback"
        p.rationale = f"{why}; " + p.rationale
        return p

    def _call(self, case: Case, allowed: list[ActionType]) -> str:
        facts = json.dumps(
            {
                "case_kind": case.kind,
                "recovery_class": case.case_class,
                "amount_rupees": case.amount_paise / 100,
                "age_days": round(case.age_s / 86_400, 1),
                "classification_rationale": (
                    case.classification.rationale if case.classification else None
                ),
                "allowed_actions": [a.value for a in allowed],
            },
            indent=2,
        )

        # Gemini has no separate system role on this endpoint, so the same
        # prompt the Anthropic planner uses is sent as system_instruction --
        # keeping one source of truth for what the model is told.
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": facts}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self.max_output_tokens,
                "responseMimeType": "application/json",
            },
        }

        req = urllib.request.Request(
            f"{API_ROOT}/{self.model}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        candidates = payload.get("candidates") or []
        if not candidates:
            raise ValueError("no candidates returned")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            # A thinking model can exhaust the output budget before writing
            # anything. Treat it as a failure rather than as an empty plan.
            raise ValueError("empty completion")
        return text
