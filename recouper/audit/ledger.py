"""Append-only, hash-chained audit ledger.

Every decision the system makes lands here: actions taken, actions denied,
actions escalated, LLM calls, provider failures, breaker trips. The ledger is
the answer to "why did this customer receive that message?" and, just as
importantly, "why did this customer receive nothing?"

Two design points:

**Denials are recorded with the same weight as actions.** A recovery system
whose refusals are invisible cannot be shown to be bounded -- you would have
to take its restraint on faith. Roughly half the interesting content of a
real run is things we decided not to do.

**Records are hash-chained.** Each entry stores the hash of the previous one,
so any edit or deletion in the middle of the file breaks every subsequent
link. This does not make the log tamper-*proof* -- anyone who can rewrite the
file can recompute the chain -- but it makes casual tampering and accidental
corruption detectable, which is the realistic threat for an operational log.
`verify()` walks the chain and reports the first break.

Format is JSON Lines: one self-contained JSON object per line. Chosen over a
database because it is append-only by nature, survives a crash mid-write
without corrupting earlier records, and can be inspected with a text editor
by a reviewer who does not want to run our code.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

GENESIS_HASH = "0" * 64


def _canonical(obj: Any) -> str:
    """Stable JSON for hashing.

    sort_keys is essential: Python preserves dict insertion order, so two
    logically identical records could serialise differently and produce
    different hashes, which would break verification for no reason.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class AuditRecord:
    seq: int
    ts: float
    actor: str  # system | llm | policy | provider | human
    event: str
    case_id: Optional[str] = None
    action: Optional[str] = None
    decision: Optional[str] = None
    reason: Optional[str] = None
    policy_checks: list[dict] = field(default_factory=list)
    idempotency_key: Optional[str] = None
    result: Optional[dict] = None
    detail: dict = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    hash: str = ""

    def payload(self) -> dict:
        """Everything the hash covers -- i.e. every field except the hash."""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "event": self.event,
            "case_id": self.case_id,
            "action": self.action,
            "decision": self.decision,
            "reason": self.reason,
            "policy_checks": self.policy_checks,
            "idempotency_key": self.idempotency_key,
            "result": self.result,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        return hashlib.sha256(_canonical(self.payload()).encode()).hexdigest()

    def to_json(self) -> str:
        d = self.payload()
        d["hash"] = self.hash
        return json.dumps(d, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "AuditRecord":
        return cls(
            seq=d["seq"], ts=d["ts"], actor=d["actor"], event=d["event"],
            case_id=d.get("case_id"), action=d.get("action"),
            decision=d.get("decision"), reason=d.get("reason"),
            policy_checks=d.get("policy_checks", []),
            idempotency_key=d.get("idempotency_key"),
            result=d.get("result"), detail=d.get("detail", {}),
            prev_hash=d.get("prev_hash", GENESIS_HASH), hash=d.get("hash", ""),
        )


@dataclass
class ChainVerification:
    ok: bool
    records_checked: int
    broken_at_seq: Optional[int] = None
    problem: Optional[str] = None

    def describe(self) -> str:
        if self.ok:
            return f"chain intact across {self.records_checked} records"
        return (
            f"CHAIN BROKEN at seq={self.broken_at_seq} after "
            f"{self.records_checked} records: {self.problem}"
        )


class AuditLedger:
    """Append-only ledger backed by a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._last_hash = GENESIS_HASH
        if self.path.exists():
            self._resume()

    def _resume(self) -> None:
        """Pick up the chain from an existing file.

        A run that crashed mid-batch must be able to continue appending
        without breaking the chain, so we read the tail rather than starting
        a fresh one.
        """
        last = None
        for rec in self.read_all():
            last = rec
        if last is not None:
            self._seq = last.seq + 1
            self._last_hash = last.hash

    def append(
        self,
        *,
        actor: str,
        event: str,
        case_id: Optional[str] = None,
        action: Optional[str] = None,
        decision: Optional[str] = None,
        reason: Optional[str] = None,
        policy_checks: Optional[list[dict]] = None,
        idempotency_key: Optional[str] = None,
        result: Optional[dict] = None,
        **detail: Any,
    ) -> AuditRecord:
        with self._lock:
            rec = AuditRecord(
                seq=self._seq,
                ts=time.time(),
                actor=actor,
                event=event,
                case_id=case_id,
                action=action,
                decision=decision,
                reason=reason,
                policy_checks=policy_checks or [],
                idempotency_key=idempotency_key,
                result=result,
                detail=detail,
                prev_hash=self._last_hash,
            )
            rec.hash = rec.compute_hash()

            # Write and flush to disk before updating in-memory state, so a
            # crash can never leave us believing we logged something we did
            # not. Losing the tail of a log is recoverable; a log that claims
            # a customer was contacted when they were not is not.
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(rec.to_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())

            self._seq += 1
            self._last_hash = rec.hash
            return rec

    def read_all(self) -> Iterator[AuditRecord]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield AuditRecord.from_dict(json.loads(line))

    def verify(self) -> ChainVerification:
        """Walk the chain and report the first break.

        Checks two things per record: that the stored hash matches a
        recomputation of its contents (catches edits), and that prev_hash
        matches the previous record's hash (catches deletions, reordering and
        insertions).
        """
        expected_prev = GENESIS_HASH
        expected_seq = 0
        n = 0

        for rec in self.read_all():
            if rec.seq != expected_seq:
                return ChainVerification(
                    False, n, rec.seq,
                    f"sequence gap: expected seq={expected_seq}, found {rec.seq} "
                    f"(a record was removed or inserted)",
                )
            if rec.prev_hash != expected_prev:
                return ChainVerification(
                    False, n, rec.seq,
                    "prev_hash does not match the preceding record's hash",
                )
            if rec.compute_hash() != rec.hash:
                return ChainVerification(
                    False, n, rec.seq,
                    "contents do not match the stored hash (record was edited)",
                )
            expected_prev = rec.hash
            expected_seq += 1
            n += 1

        return ChainVerification(True, n)

    # --- reporting ---------------------------------------------------------

    def case_timeline(self, case_id: str) -> list[AuditRecord]:
        return [r for r in self.read_all() if r.case_id == case_id]

    def denial_counts(self) -> dict[str, int]:
        """How often each rule blocked something.

        The headline number for "the bounds actually bind". A rule that never
        fires across a whole batch is either dead code or an untested claim,
        and either way we want to see it.
        """
        counts: dict[str, int] = {}
        for rec in self.read_all():
            if rec.decision in ("deny", "escalate"):
                for check in rec.policy_checks:
                    if check.get("decision") in ("deny", "escalate"):
                        rid = check.get("rule_id", "unknown")
                        counts[rid] = counts.get(rid, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def event_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in self.read_all():
            counts[rec.event] = counts.get(rec.event, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
