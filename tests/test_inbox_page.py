"""Import-light tests for the full Inbox page card-builder (Task 17).

Mirrors test_recovery_panel.py: exercise the PURE card-builder helper
(_inbox_card_model) + the pure history/triage splitters, WITHOUT standing up
NiceGUI.
"""
from __future__ import annotations

import pytest

from systemu.interface.command.gate import GateDescriptor


def test_inbox_page_module_imports():
    from systemu.interface.pages import inbox_page
    assert callable(inbox_page.build_inbox_page)


# ── _inbox_card_model — the unified card (spec §4.3) ──────────────────────────

def _model(descriptor):
    from systemu.interface.pages.inbox_page import _inbox_card_model
    return _inbox_card_model(descriptor)


def test_high_risk_descriptor_gets_destructive_treatment():
    d = GateDescriptor.from_forge({"id": "tool_x", "name": "fetch",
                                   "description": "fetch json"})
    assert d.risk == "high"
    m = _model(d)
    # High-risk gets the distinct destructive treatment flag (visually distinct
    # card + the affirmative option styled danger).
    assert m["destructive"] is True
    # Carries the explicit "what Approve does" text.
    assert m["what_approve_does"] == d.what_approve_does
    assert m["what_approve_does"]  # non-empty
    # Safe-default is highlighted and is the descriptor's safe_default.
    assert m["safe_default"] == d.safe_default == "Skip"
    assert m["dedup"] == d.dedup


def test_low_risk_descriptor_not_destructive():
    d = GateDescriptor(title="low one", risk="low",
                       options=["Reject", "Approve"], safe_default="Reject",
                       what_approve_does="does the thing", dedup="x:1")
    m = _model(d)
    assert m["destructive"] is False
    assert m["safe_default"] == "Reject"
    assert m["affirmative"] == "Approve"
    assert m["inspect"] == d.inspect


def test_medium_risk_descriptor_not_destructive():
    class _S:
        id = "scr_1"
        name = "routine"
    d = GateDescriptor.from_scroll(_S(), summary="extract")
    assert d.risk == "medium"
    m = _model(d)
    assert m["destructive"] is False
    assert m["what_approve_does"]  # carried


def test_card_model_marks_affirmative_and_safe_default_options():
    d = GateDescriptor(title="t", risk="high",
                       options=["Dismiss", "Approve & Install"],
                       safe_default="Dismiss",
                       what_approve_does="installs", dedup="dep:requests")
    m = _model(d)
    assert m["affirmative"] == "Approve & Install"
    assert m["safe_default"] == "Dismiss"
    assert m["destructive"] is True


# ── pure history / triage splitters ───────────────────────────────────────────

def test_resolved_gate_rows_filters_to_resolved_gates():
    from systemu.interface.pages.inbox_page import _resolved_gate_rows
    rows = [
        {"id": "a", "status": "resolved", "context": {"kind": "gate"},
         "title": "T", "choice": "Approve"},
        {"id": "b", "status": "pending", "context": {"kind": "gate"}},
        {"id": "c", "status": "resolved", "context": {"kind": "harness_review"}},
        {"id": "d", "status": "resolved", "context": {"kind": "gate"},
         "title": "U", "choice": "Reject"},
    ]
    out = _resolved_gate_rows(rows)
    assert [r["id"] for r in out] == ["a", "d"]


def test_resolved_gate_rows_handles_missing_context():
    from systemu.interface.pages.inbox_page import _resolved_gate_rows
    rows = [{"id": "a", "status": "resolved"}]  # no context key
    assert _resolved_gate_rows(rows) == []
