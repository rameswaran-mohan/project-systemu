"""v0.9.41 Bug 15 follow-up — cap-denied requests are a first-class taxonomy entry.

A per-run-cap DENY (the over-delegation signal) previously wrote NO ledger row
(only the GRANT path logs, via materialise), so cap-denied requests vanished from
the request-outcome denominator. Now the loop records the arb row with a cap
marker and reconciliation classifies it as the dedicated ``cap_exceeded``
category (outcome ``denied_cap``) — never folded into ``wasted_request``.
"""
from __future__ import annotations

import inspect
import json

from systemu.runtime.governor import Governor
from systemu.runtime.failure_classifier import classify_pull_failure, CATEGORIES
from systemu.runtime.harness_arbiter import arbitrate
from systemu.runtime.harness_policy import HarnessPolicy
from systemu.core.models import HarnessRequest, HarnessKind, HarnessDecision
from systemu.vault.vault import Vault


# ── classifier: cap_exceeded is dedicated + highest precedence for a deny ─────

def test_cap_exceeded_is_a_category():
    assert "cap_exceeded" in CATEGORIES

def test_classify_cap_exceeded_precedence():
    # even with a viable fallback (would be wasted_request) cap wins — it's an
    # over-delegation signal, not a fallback judgement.
    assert classify_pull_failure(attempts_before=5, decision="deny", fallback_ok=True,
                                 used_after_grant=None, kind="subagent",
                                 cap_exceeded=True) == "cap_exceeded"
    # even at attempts_before=0 for a tool (would be premature) cap still wins.
    assert classify_pull_failure(attempts_before=0, decision="deny", fallback_ok=False,
                                 used_after_grant=None, kind="tool",
                                 cap_exceeded=True) == "cap_exceeded"
    # without the flag, a deny+fallback is still wasted_request (unchanged).
    assert classify_pull_failure(attempts_before=5, decision="deny", fallback_ok=True,
                                 used_after_grant=None, kind="subagent") == "wasted_request"


# ── arbiter: the cap DENY carries the cap_exceeded marker ────────────────────

def test_cap_deny_verdict_is_flagged():
    policy = HarnessPolicy(max_requests_per_run=8)
    req = HarnessRequest(kind=HarnessKind.SUBAGENT, spec={"tasks": ["t"]},
                         rationale="delegate", blocking=True)
    result = arbitrate(req, policy, {"requests_this_run": 8, "subagent_depth": 0})
    assert result["verdict"].decision == HarnessDecision.DENY
    assert result["verdict"].cap_exceeded is True
    # a below-cap request is NOT flagged
    ok = arbitrate(req, policy, {"requests_this_run": 2, "subagent_depth": 0})
    assert getattr(ok["verdict"], "cap_exceeded", False) is False


# ── reconciliation: cap-deny row → denied_cap / cap_exceeded ─────────────────

def _vault(tmp_path):
    (tmp_path / "harness_ledger").mkdir(parents=True, exist_ok=True)
    return Vault(str(tmp_path))

def test_reconcile_cap_deny_row():
    rows = [{"request": {"request_id": "cap1", "kind": "subagent", "attempts_before": 0},
             "verdict": {"decision": "deny"},
             "outcome": {"cap_exceeded": True}, "execution_id": "e"}]
    ev = Governor.reconcile_outcomes(rows, set(), run_success=True)
    assert ev[0]["outcome"] == "denied_cap"
    assert ev[0]["pull_failure_category"] == "cap_exceeded"   # NOT wasted_request

def test_reconcile_regular_deny_unaffected():
    rows = [{"request": {"request_id": "d1", "kind": "tool", "attempts_before": 3},
             "verdict": {"decision": "deny"}, "outcome": {}, "execution_id": "e"}]
    ev = Governor.reconcile_outcomes(rows, set(), run_success=True)
    assert ev[0]["outcome"] == "denied_fallback_ok"          # cap marker absent

def test_denominator_completes_grants_plus_cap_denies(tmp_path):
    # Mirrors the smoke: a bounded SUB-AGENT run-tree with 8 grants + 2 cap-denies.
    # Before v0.9.41 the 2 cap-denies wrote no row → denominator 8 (incomplete).
    # Now all 10 requests reconcile: 8 granted + 2 cap_exceeded.
    g = Governor(); v = _vault(tmp_path); root = "exec_root"
    for i in range(8):
        eid = f"exec_g{i}"
        g.next_runtree_request(root, eid, v)
        led = g.ledger_path(eid, v); led.parent.mkdir(parents=True, exist_ok=True)
        led.write_text(json.dumps({"request": {"request_id": f"g{i}", "kind": "subagent",
                        "attempts_before": 2}, "verdict": {"decision": "grant"},
                        "outcome": {"materialised": True}, "execution_id": eid}) + "\n",
                       encoding="utf-8")
    for i in range(2):  # the 2 over-cap requests, now recorded with the marker
        eid = f"exec_cap{i}"
        g.next_runtree_request(root, eid, v)
        led = g.ledger_path(eid, v); led.parent.mkdir(parents=True, exist_ok=True)
        led.write_text(json.dumps({"request": {"request_id": f"cap{i}", "kind": "subagent",
                        "attempts_before": 0}, "verdict": {"decision": "deny"},
                        "outcome": {"cap_exceeded": True}, "execution_id": eid}) + "\n",
                       encoding="utf-8")
    terminal = "exec_term"
    also = g.runtree_execution_ids(root, v)   # all 10 requesting execs
    n = g.write_outcome_reconciliation(terminal, set(), run_success=True, vault=v, also_ids=also)
    assert n == 10, f"complete denominator = 10 (8 grants + 2 cap-denies), got {n}"
    led = g.ledger_path(terminal, v)
    cats = {}
    for l in led.read_text(encoding="utf-8").splitlines():
        if l.strip() and json.loads(l).get("event_type") == "request-outcome":
            r = json.loads(l); cats[r["pull_failure_category"]] = cats.get(r["pull_failure_category"], 0) + 1
    assert cats.get("cap_exceeded") == 2, cats           # over-delegation surfaced
    outs = {json.loads(l)["outcome"] for l in led.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("event_type") == "request-outcome"}
    assert "denied_cap" in outs and "granted" in outs


# ── seam: the loop records the cap-deny row ──────────────────────────────────

def test_execute_records_cap_deny_row():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    assert 'getattr(_verdict, "cap_exceeded"' in src      # detects the cap deny
    assert '{"cap_exceeded": True}' in src                # records it with the marker
