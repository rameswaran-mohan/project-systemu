"""Task 4 — Harness surface-only routing into the unified Decisions Inbox.

surface_harness_request must post the escalated HarnessRequest as a
``kind="gate"`` / ``gate_type="harness"`` row so that
``InboxQueue.list_descriptors()`` surfaces it (today's ``list_descriptors``
filters ``ctx.get("kind") == "gate"`` and skips the legacy
``kind="harness_review"`` rows).

The re-tag must PRESERVE the harness-specific context fields the future
grant-resume work + current renderers depend on: execution_id / request_id /
harness_kind / spec (and friends). The dedup key
``harness:<execution_id>:<request_id>`` and the options
``["Deny", "Approve", "Edit spec"]`` are unchanged.

resolve_gate's harness branch stays render-only (QUEUED) — NOT exercised here.
"""

from __future__ import annotations

import uuid


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures (mirror tests/test_v0_9_7_harness_review.py shapes)
# ─────────────────────────────────────────────────────────────────────────────

def _make_request(
    *,
    kind_str: str = "tool",
    request_id: str | None = None,
    rationale: str = "Need a new tool",
    urgency: str = "normal",
    blocking: bool = True,
    spec: dict | None = None,
):
    """Build a minimal HarnessRequest-like object via the real model."""
    from systemu.core.models import HarnessKind, HarnessRequest

    return HarnessRequest(
        request_id=request_id or ("hreq_" + uuid.uuid4().hex[:8]),
        kind=HarnessKind(kind_str),
        spec=spec or {"name": "my_tool"},
        rationale=rationale,
        urgency=urgency,
        blocking=blocking,
    )


def _make_verdict(*, decision_str: str = "escalate", risk_band: str = "high",
                  rationale: str = "risky"):
    """Build a minimal HarnessVerdict-like object via the real model."""
    from systemu.core.models import HarnessDecision, HarnessVerdict, RiskBand

    return HarnessVerdict(
        decision=HarnessDecision(decision_str),
        risk_band=RiskBand(risk_band),
        rationale=rationale,
    )


def _real_vault(tmp_path):
    """A real FileVault so list_descriptors() walks the actual decisions store."""
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault

    return FileVault(Vault(str(tmp_path / "v")))


# ─────────────────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_harness_surfaces_as_gate_in_inbox(tmp_path):
    """surface_harness_request posts a kind='gate' harness row the Inbox lists."""
    from systemu.interface.harness_review import surface_harness_request
    from systemu.interface.command.inbox import InboxQueue

    vault = _real_vault(tmp_path)
    request = _make_request(kind_str="tool", request_id="hreq_route01",
                            spec={"name": "csv_summariser"})
    verdict = _make_verdict(decision_str="escalate", risk_band="high")
    execution_id = "exec_route"

    surface_harness_request(request, verdict, execution_id=execution_id,
                            vault=vault)

    descs = InboxQueue(vault).list_descriptors()
    expected_dedup = f"harness:{execution_id}:{request.request_id}"
    matches = [d for _, d in descs if d.dedup == expected_dedup]
    assert matches, (
        f"expected a list_descriptors() row with dedup {expected_dedup!r}; "
        f"got {[d.dedup for _, d in descs]!r}"
    )
    desc = matches[0]
    assert desc.options == ["Deny", "Approve", "Edit spec"]


def test_harness_gate_preserves_harness_context_fields(tmp_path):
    """The stored decision context still carries the harness-specific fields the
    future grant-resume work + current renderers need, alongside kind='gate'."""
    from systemu.interface.harness_review import surface_harness_request
    from systemu.approval.decision_queue import OperatorDecisionQueue

    vault = _real_vault(tmp_path)
    request = _make_request(kind_str="tool", request_id="hreq_ctx02",
                            spec={"name": "my_tool", "arg": 1})
    verdict = _make_verdict(decision_str="escalate", risk_band="high")
    execution_id = "exec_ctx"

    dec_id = surface_harness_request(request, verdict,
                                     execution_id=execution_id, vault=vault)

    decision = OperatorDecisionQueue(vault).get_resolved_choice  # noqa: F841
    stored = vault.get_decision(dec_id)
    ctx = stored.context or {}

    # Re-tagged to a gate so list_descriptors surfaces it.
    assert ctx["kind"] == "gate"
    assert ctx["gate_type"] == "harness"

    # Harness-specific fields preserved for the (future) execute work.
    assert ctx["execution_id"] == execution_id
    assert ctx["request_id"] == "hreq_ctx02"
    assert ctx["harness_kind"] == "tool"
    assert ctx["spec"] == {"name": "my_tool", "arg": 1}


def test_harness_surface_dedups(tmp_path):
    """Repeated surfacing for the same escalation returns the existing row."""
    from systemu.interface.harness_review import surface_harness_request
    from systemu.interface.command.inbox import InboxQueue

    vault = _real_vault(tmp_path)
    request = _make_request(request_id="hreq_dup")
    verdict = _make_verdict()
    execution_id = "exec_dup"

    id1 = surface_harness_request(request, verdict,
                                  execution_id=execution_id, vault=vault)
    id2 = surface_harness_request(request, verdict,
                                  execution_id=execution_id, vault=vault)
    assert id1 == id2

    descs = InboxQueue(vault).list_descriptors()
    expected_dedup = f"harness:{execution_id}:{request.request_id}"
    assert sum(1 for _, d in descs if d.dedup == expected_dedup) == 1
