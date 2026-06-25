"""The Inbox routes a harness CAPABILITY gate through render_decision_card (so the
amend-then-approve Deny/Approve/Edit affordance appears on the primary surface),
while INPUT and non-harness gates keep the unified triage card.

Regression guard for the gap found by running the dashboard: render_inbox_gate_cards
previously sent every gate to _render_unified_card, which has no Edit button.
"""
from test_harness_grant_reconciler import _make_vault   # bare import: no tests/__init__.py
from systemu.approval.decision_queue import OperatorDecisionQueue
import systemu.interface.pages.insights as insights_mod
import systemu.interface.pages.inbox_page as inbox_mod


def _post(q, *, kind, harness_kind, eid, spec):
    return q.post(
        title=f"Harness request: {harness_kind}", body="?",
        options=["Deny", "Approve"],
        context={"kind": "gate", "gate_type": kind, "harness_kind": harness_kind,
                 "execution_id": eid, "activity_id": f"act_{eid}",
                 "shadow_id": f"sh_{eid}", "request_id": f"hreq_{eid}",
                 "risk_band": "high", "spec": spec},
        dedup_key=f"{kind}:{eid}:hreq_{eid}")


def test_capability_gate_routes_to_decision_card_input_stays_unified(tmp_path, monkeypatch):
    vlt = _make_vault(tmp_path)
    q = OperatorDecisionQueue(vlt)
    _post(q, kind="harness", harness_kind="tool", eid="t1", spec={"name": "fetch_sha"})
    _post(q, kind="harness", harness_kind="input", eid="i1", spec={"question": "which?"})

    routed = {"card": [], "unified": []}
    # render_inbox_gate_cards does a LOCAL `from ...insights import render_decision_card`,
    # so patching the attribute on the insights module is picked up at call time.
    monkeypatch.setattr(
        insights_mod, "render_decision_card",
        lambda card, queue, on_resolved: routed["card"].append(
            ((card or {}).get("context") or {}).get("harness_kind")))
    monkeypatch.setattr(
        inbox_mod, "_render_unified_card",
        lambda dec_id, descriptor, *, vault, on_resolved: routed["unified"].append(dec_id))

    n = inbox_mod.render_inbox_gate_cards(vlt, on_resolved=lambda: None)
    assert n == 2
    assert routed["card"] == ["tool"]        # capability gate → full card (gets Edit)
    assert len(routed["unified"]) == 1       # input gate → unified card (unchanged)
