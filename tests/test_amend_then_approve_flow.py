"""Task 6/10: the reconciler approve-branch routes through ``Governor.grant`` and
honours an operator's ``amended_spec`` (amend-then-approve).

Reuses helper NAMES (``_FakeSupervisor`` / ``_make_vault`` / ``_seed_snapshot``)
from ``tests/test_harness_grant_reconciler.py``. NOTE: that module's autouse
``_patch_governor`` fixture (which swaps in a force-granting ``_FakeGovernor``) is
MODULE-SCOPED to that file and does NOT apply here — so these tests exercise the
REAL ``Governor.grant`` end-to-end through the reconciler (the arbiter actually
runs). COMPUTE/ACCESS materialise need no real forge, so a real Vault suffices.
"""
import json  # noqa: F401  (kept per plan; structured-choice flows may use it)

from systemu.scheduler.jobs import reconcile_resolved_harness_grants
# Reuse the existing reconciler test scaffolding. NB: this repo has no
# ``tests/__init__.py`` and pytest imports test modules at top level, so the
# sibling is imported by bare module name (matching test_v0937_mcp_runtime_*).
from test_harness_grant_reconciler import (  # noqa: E402
    _FakeSupervisor, _make_vault, _seed_snapshot,
)
from systemu.approval.decision_queue import OperatorDecisionQueue


def _post_capability_gate(vault, *, amended_spec=None):
    q = OperatorDecisionQueue(vault)
    ctx = {"kind": "gate", "gate_type": "harness", "harness_kind": "compute",
           "execution_id": "exec_c", "activity_id": "act_c", "shadow_id": "sh_c",
           "request_id": "hreq_c", "spec": {"budget_fraction": 0.2},
           "risk_band": "medium", "arb_context": {"requests_this_run": 0}}
    if amended_spec is not None:
        ctx["amended_spec"] = amended_spec
    did = q.post(title="Harness request: compute", body="?",
                 options=["Deny", "Approve"], context=ctx,
                 dedup_key="harness:exec_c:hreq_c")
    q.resolve(did, choice="Approve")
    return did


def test_reconciler_grants_amended_spec(tmp_path):
    vlt = _make_vault(tmp_path)
    data_dir = _seed_snapshot(tmp_path, execution_id="exec_c", shadow_id="sh_c",
                              scroll_id="sc_c", activity_id="act_c")
    _post_capability_gate(vlt, amended_spec={"budget_fraction": 0.4})
    sup = _FakeSupervisor()
    n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
    assert n == 1 and len(sup.calls) == 1
    gp = sup.calls[0]["grant_payload"]
    assert gp["kind"] == "compute"
    assert gp.get("granted") is True       # materialised, not denied


def test_surface_persists_arb_context(tmp_path):
    from systemu.interface.harness_review import surface_harness_request
    from systemu.core.models import (HarnessRequest, HarnessKind, HarnessVerdict,
                                     HarnessDecision, RiskBand)
    vlt = _make_vault(tmp_path)
    req = HarnessRequest(kind=HarnessKind.COMPUTE, spec={"budget_fraction": 0.2})
    vd = HarnessVerdict(request_id=req.request_id, decision=HarnessDecision.ESCALATE,
                        risk_band=RiskBand.MEDIUM, rationale="ask")
    did = surface_harness_request(req, vd, execution_id="exec_a", activity_id="act_a",
                                  shadow_id="sh_a", vault=vlt,
                                  arb_context={"requests_this_run": 3, "subagent_depth": 0})
    dec = vlt.get_decision(did)
    assert (dec.context or {}).get("arb_context") == {"requests_this_run": 3,
                                                      "subagent_depth": 0}


def _post_access_amend(vault, *, eid, confirmed):
    """Post an ACCESS gate amended read->write (a real risk-band increase:
    write=HIGH, read=MEDIUM), optionally with a typed-confirm record."""
    q = OperatorDecisionQueue(vault)
    ctx = {"kind": "gate", "gate_type": "harness", "harness_kind": "access",
           "execution_id": eid, "activity_id": f"act_{eid}", "shadow_id": f"sh_{eid}",
           "request_id": f"hreq_{eid}",
           "spec": {"access_type": "read", "resource": "notes"},
           "amended_spec": {"access_type": "write", "resource": "notes"},
           "arb_context": {"requests_this_run": 0}}
    if confirmed:
        ctx["amend_band_escalation"] = {"from": "medium", "to": "high", "confirmed": True}
    did = q.post(title="Harness request: access", body="?",
                 options=["Deny", "Approve"], context=ctx,
                 dedup_key=f"harness:{eid}:hreq_{eid}")
    q.resolve(did, choice="Approve")
    return did


def test_reconciler_denies_unconfirmed_band_increase(tmp_path):
    # REAL Governor.grant: read->write raises the band; no confirmation -> denied.
    vlt = _make_vault(tmp_path)
    data_dir = _seed_snapshot(tmp_path, execution_id="exec_b", shadow_id="sh_exec_b",
                              scroll_id="sc_b", activity_id="act_exec_b")
    _post_access_amend(vlt, eid="exec_b", confirmed=False)
    sup = _FakeSupervisor()
    reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
    assert len(sup.calls) == 1
    gp = sup.calls[0]["grant_payload"]
    assert gp.get("denied") is True
    assert gp.get("amend_rejected") is True


def test_reconciler_grants_confirmed_band_increase(tmp_path):
    # REAL Governor.grant: same read->write, but WITH the typed-confirm record ->
    # materialises (ACCESS advisory lease), not denied.
    vlt = _make_vault(tmp_path)
    data_dir = _seed_snapshot(tmp_path, execution_id="exec_d", shadow_id="sh_exec_d",
                              scroll_id="sc_d", activity_id="act_exec_d")
    _post_access_amend(vlt, eid="exec_d", confirmed=True)
    sup = _FakeSupervisor()
    reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
    assert len(sup.calls) == 1
    gp = sup.calls[0]["grant_payload"]
    assert gp.get("denied") is not True
    assert gp.get("kind") == "access"
