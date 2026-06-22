"""Plan 0 Task 1.6 — terminal-pass request-outcome reconciliation (Governor)."""
import json
import pytest

from systemu.runtime.governor import Governor
from systemu.vault.vault import Vault


def _rows():
    return [
        {"request": {"request_id": "r1"}, "verdict": {"decision": "grant"},
         "outcome": {"tool": "t_used"}, "execution_id": "e1"},
        {"request": {"request_id": "r2"}, "verdict": {"decision": "grant"},
         "outcome": {"tool": "t_unused"}, "execution_id": "e1"},
        {"request": {"request_id": "r3"}, "verdict": {"decision": "deny"},
         "outcome": {}, "execution_id": "e1"},
        {"request": {"request_id": "r4"}, "verdict": {"decision": "escalate"},
         "outcome": {}, "execution_id": "e1"},
        {"event_type": "lease-mint", "lease_id": "l1"},   # must be skipped
    ]


def test_reconcile_outcomes_pure():
    ev = Governor.reconcile_outcomes(_rows(), {"t_used"}, run_success=True)
    by_id = {e["request_id"]: e["outcome"] for e in ev}
    assert by_id == {
        "r1": "granted_used", "r2": "granted_unused",
        "r3": "denied_fallback_ok", "r4": "escalate_unresolved",
    }
    assert all(e["event_type"] == "request-outcome" for e in ev)
    assert len(ev) == 4  # the lease-mint event row is skipped


def test_reconcile_deny_failed_when_run_failed():
    ev = Governor.reconcile_outcomes(
        [{"request": {"request_id": "rd"}, "verdict": {"decision": "deny"},
          "outcome": {}, "execution_id": "e"}], set(), run_success=False)
    assert ev[0]["outcome"] == "denied_fallback_failed"


def test_reconcile_empty_and_malformed_safe():
    assert Governor.reconcile_outcomes([], set()) == []
    assert Governor.reconcile_outcomes([None, {}, {"request": {}}], set()) == []


def test_ledger_entry_includes_pull_decision_fields():
    """Task 1.7(a) — the ledger request entry carries attempts_before + confidence
    so reconciliation/CGB can classify pull-decision failures."""
    from systemu.core.models import (
        HarnessRequest, HarnessVerdict, HarnessKind, HarnessDecision,
    )
    req = HarnessRequest(kind=HarnessKind.TOOL, attempts_before_request=3, confidence=0.7)
    verd = HarnessVerdict(decision=HarnessDecision.GRANT)
    entry = Governor._ledger_entry(req, verd, {"materialised": True}, "e1")
    assert entry["request"]["attempts_before"] == 3
    assert entry["request"]["confidence"] == 0.7


def test_reconcile_includes_pull_failure_category():
    """Task 1.7(b) — reconcile_outcomes attaches a pull-failure taxonomy category."""
    rows = [
        {"request": {"request_id": "g_used", "attempts_before": 2},
         "verdict": {"decision": "grant"}, "outcome": {"tool": "tu"}, "execution_id": "e"},
        {"request": {"request_id": "g_unused", "attempts_before": 2},
         "verdict": {"decision": "grant"}, "outcome": {"tool": "tx"}, "execution_id": "e"},
        {"request": {"request_id": "premature", "attempts_before": 0, "kind": "tool"},
         "verdict": {"decision": "grant"}, "outcome": {"tool": "tp"}, "execution_id": "e"},
        {"request": {"request_id": "deny", "attempts_before": 3},
         "verdict": {"decision": "deny"}, "outcome": {}, "execution_id": "e"},
    ]
    ev = Governor.reconcile_outcomes(rows, {"tu"}, run_success=True)  # only "tu" used
    cat = {e["request_id"]: e.get("pull_failure_category") for e in ev}
    assert cat["g_used"] == "unknown"               # used grant, enough attempts
    assert cat["g_unused"] == "unused_grant"        # granted but never invoked
    assert cat["premature"] == "premature_request"  # attempts_before < 1 wins
    assert cat["deny"] == "wasted_request"          # denied but fallback was viable


def test_write_outcome_reconciliation_appends_to_ledger(tmp_path):
    for sub in ["harness_ledger"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    vault = Vault(str(tmp_path))
    g = Governor()
    exec_id = "exec_recon"
    # seed the ledger with two arbitrated requests
    led = g.ledger_path(exec_id, vault)
    led.parent.mkdir(parents=True, exist_ok=True)
    with led.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(_rows()[0]) + "\n")   # grant t_used
        fh.write(json.dumps(_rows()[2]) + "\n")   # deny
    n = g.write_outcome_reconciliation(exec_id, {"t_used"}, run_success=True, vault=vault)
    assert n == 2
    lines = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines() if l.strip()]
    outcomes = [l for l in lines if l.get("event_type") == "request-outcome"]
    assert {o["request_id"]: o["outcome"] for o in outcomes} == {
        "r1": "granted_used", "r3": "denied_fallback_ok"}
