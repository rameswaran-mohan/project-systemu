"""Task 2 (harness grant-resume) — stamp resume coords into the gate context.

`surface_harness_request` must accept ``activity_id`` / ``shadow_id`` kwargs and
preserve them in the posted decision's context (alongside the existing
kind=="gate" / gate_type=="harness" markers and the harness fields). The
daemon harness-grant reconciler needs these coords to call
``Supervisor.resume_after_grant(execution_id=, activity_id=, shadow_id=, ...)``.

Uses a real FileVault(Vault(tmp)) and reads the posted decision back end-to-end.
"""
from __future__ import annotations

import uuid

import pytest


def _make_request(*, kind_str: str = "tool", request_id: str | None = None):
    from systemu.core.models import HarnessKind, HarnessRequest

    return HarnessRequest(
        request_id=request_id or ("hreq_" + uuid.uuid4().hex[:8]),
        kind=HarnessKind(kind_str),
        spec={"name": "my_tool"},
        rationale="need a tool",
        urgency="normal",
        blocking=True,
    )


def _make_verdict(*, decision_str: str = "escalate", risk_band: str = "high"):
    from systemu.core.models import HarnessDecision, HarnessVerdict, RiskBand

    return HarnessVerdict(
        decision=HarnessDecision(decision_str),
        risk_band=RiskBand(risk_band),
        rationale="risky",
    )


def _file_vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault

    return FileVault(Vault(tmp_path / "vault"))


def test_surface_stamps_activity_and_shadow_coords(tmp_path):
    from systemu.interface.harness_review import surface_harness_request

    vault = _file_vault(tmp_path)
    request = _make_request(kind_str="tool", request_id="hreq_coord")
    verdict = _make_verdict()

    decision_id = surface_harness_request(
        request,
        verdict,
        execution_id="exec_coord",
        activity_id="act_1",
        shadow_id="sh_1",
        vault=vault,
    )

    decision = vault.get_decision(decision_id)
    ctx = decision.context or {}

    # Resume coords stamped.
    assert ctx["activity_id"] == "act_1"
    assert ctx["shadow_id"] == "sh_1"
    # Existing gate markers still present.
    assert ctx["kind"] == "gate"
    assert ctx["gate_type"] == "harness"
    # Existing harness fields still preserved.
    assert ctx["execution_id"] == "exec_coord"
    assert ctx["request_id"] == "hreq_coord"
    assert ctx["harness_kind"] == "tool"


def test_surface_coords_default_empty_when_omitted(tmp_path):
    """Back-compat: omitting the new kwargs is fine and defaults to empty strings."""
    from systemu.interface.harness_review import surface_harness_request

    vault = _file_vault(tmp_path)
    request = _make_request(request_id="hreq_default")
    verdict = _make_verdict()

    decision_id = surface_harness_request(
        request, verdict, execution_id="exec_default", vault=vault
    )

    decision = vault.get_decision(decision_id)
    ctx = decision.context or {}
    assert ctx.get("activity_id", "") == ""
    assert ctx.get("shadow_id", "") == ""
    assert ctx["kind"] == "gate"
    assert ctx["gate_type"] == "harness"
