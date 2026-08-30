"""Tests for the audit ledger.

The tampering tests matter most. A hash chain nobody has tried to break is
just a decorative field called `prev_hash` -- these tests are what let us
claim it detects anything.
"""

import json

import pytest

from recouper.audit.ledger import GENESIS_HASH, AuditLedger


@pytest.fixture
def ledger(tmp_path):
    return AuditLedger(tmp_path / "audit.jsonl")


def test_first_record_links_to_genesis(ledger):
    rec = ledger.append(actor="system", event="batch_started")
    assert rec.seq == 0
    assert rec.prev_hash == GENESIS_HASH
    assert len(rec.hash) == 64


def test_records_chain_together(ledger):
    a = ledger.append(actor="system", event="one")
    b = ledger.append(actor="system", event="two")
    c = ledger.append(actor="system", event="three")
    assert b.prev_hash == a.hash
    assert c.prev_hash == b.hash
    assert [r.seq for r in (a, b, c)] == [0, 1, 2]


def test_clean_chain_verifies(ledger):
    for i in range(20):
        ledger.append(actor="system", event=f"event_{i}")
    v = ledger.verify()
    assert v.ok
    assert v.records_checked == 20


# --- tampering --------------------------------------------------------------


def test_editing_a_record_breaks_the_chain(ledger):
    """The core claim: you cannot quietly change history.

    Simulates someone editing the log to hide that a customer on the
    do-not-contact list was messaged.
    """
    for i in range(5):
        ledger.append(actor="policy", event="action_denied", reason=f"reason {i}")

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    doctored = json.loads(lines[2])
    doctored["reason"] = "actually this was fine"
    lines[2] = json.dumps(doctored)
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    v = ledger.verify()
    assert not v.ok
    assert v.broken_at_seq == 2
    assert "edited" in v.problem


def test_deleting_a_record_breaks_the_chain(ledger):
    """Deletion is the likelier tampering mode -- removing an inconvenient
    entry entirely rather than rewriting one."""
    for i in range(5):
        ledger.append(actor="system", event=f"event_{i}")

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    del lines[2]
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    v = ledger.verify()
    assert not v.ok
    assert v.broken_at_seq == 3


def test_reordering_records_breaks_the_chain(ledger):
    for i in range(5):
        ledger.append(actor="system", event=f"event_{i}")

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[3] = lines[3], lines[1]
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not ledger.verify().ok


def test_appending_a_forged_record_breaks_the_chain(ledger):
    """A forger who does not know the chain cannot simply add a line."""
    ledger.append(actor="system", event="real")
    forged = {
        "seq": 1, "ts": 0, "actor": "system", "event": "forged",
        "case_id": None, "action": None, "decision": None, "reason": None,
        "policy_checks": [], "idempotency_key": None, "result": None,
        "detail": {}, "prev_hash": GENESIS_HASH, "hash": "f" * 64,
    }
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(forged) + "\n")

    assert not ledger.verify().ok


# --- durability -------------------------------------------------------------


def test_ledger_resumes_an_existing_chain(tmp_path):
    """A crashed batch must be able to continue without breaking the chain."""
    path = tmp_path / "audit.jsonl"
    first = AuditLedger(path)
    first.append(actor="system", event="before_crash")
    first.append(actor="system", event="also_before")

    resumed = AuditLedger(path)  # fresh process
    rec = resumed.append(actor="system", event="after_restart")

    assert rec.seq == 2
    assert resumed.verify().ok


# --- reporting --------------------------------------------------------------


def test_denials_are_recorded_and_countable(ledger):
    """Refusals must be as visible as actions, or restraint is unfalsifiable."""
    ledger.append(
        actor="policy", event="action_denied", case_id="pay_1",
        action="send_reminder", decision="deny", reason="on DNC list",
        policy_checks=[
            {"rule_id": "do_not_contact", "decision": "deny", "reason": "DNC"},
            {"rule_id": "quiet_hours", "decision": "allow", "reason": "ok"},
        ],
    )
    ledger.append(
        actor="policy", event="action_denied", case_id="pay_2",
        decision="deny",
        policy_checks=[
            {"rule_id": "do_not_contact", "decision": "deny", "reason": "DNC"}
        ],
    )

    counts = ledger.denial_counts()
    assert counts["do_not_contact"] == 2
    assert "quiet_hours" not in counts, "allowed checks must not count as denials"


def test_case_timeline_reconstructs_one_cases_history(ledger):
    ledger.append(actor="system", event="case_opened", case_id="pay_1")
    ledger.append(actor="system", event="noise", case_id="pay_2")
    ledger.append(actor="policy", event="action_denied", case_id="pay_1")
    ledger.append(actor="system", event="case_closed", case_id="pay_1")

    timeline = ledger.case_timeline("pay_1")
    assert [r.event for r in timeline] == ["case_opened", "action_denied", "case_closed"]


def test_hash_is_stable_regardless_of_field_order(ledger):
    """Guards the canonical-JSON choice.

    Without sort_keys, two logically identical records could hash
    differently and verification would fail for no real reason.
    """
    rec = ledger.append(actor="system", event="x", meta={"b": 1, "a": 2})
    assert rec.compute_hash() == rec.hash

    # Same content, rebuilt with the opposite insertion order. Python
    # preserves insertion order, so without sort_keys these would serialise
    # differently and hash differently.
    rec.detail = {"meta": {"a": 2, "b": 1}}
    assert rec.compute_hash() == rec.hash
