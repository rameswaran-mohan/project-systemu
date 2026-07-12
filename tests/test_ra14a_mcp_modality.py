"""R-A14a slice 2 — the ``mcp`` ActuationModality impl (real-path).

Two contracts under test:
  * ``execute()`` routes THROUGH the gated MCP chokepoint (never bypasses): an
    un-allowlisted call is REFUSED (L2), and an L3 gate DENY (``PendingOperatorDecision``)
    PROPAGATES (never swallowed).
  * ``capture_evidence()`` turns a KNOWN-mutation MCP result into an ExternalEvidence by
    REUSING the money-move-safe verifier — an api_readback directive echoed by an
    injected client CONFIRMS; a READ returns None.
"""
from __future__ import annotations

import types

import pytest


class _Vault:
    """Minimal vault stand-in: connections.py only reads ``.root``."""
    def __init__(self, root):
        self.root = str(root)


class _EchoReadbackClient:
    """A mock api_readback transport that ECHOES the expected token as an observed
    token — the deterministic ground truth the hardened path matches."""
    def __init__(self, echo_tokens):
        self._echo = list(echo_tokens)
        self.urls = []

    def readback(self, url):
        self.urls.append(url)
        return {"observed_tokens": list(self._echo),
                "response_body": "resource present: " + " ".join(self._echo)}


def _objective(goal="open a GitHub issue for the login bug", **ov):
    from systemu.core.models import Objective
    base = dict(id=7, goal=goal, success_criteria="issue visible via readback",
                requires_external_verification=False)
    base.update(ov)
    return Objective(**base)


# ─────────────────────────────────────────────────────────────────────────────
#  execute() routes THROUGH the gate
# ─────────────────────────────────────────────────────────────────────────────

def test_execute_l2_allowlist_refusal_propagates(tmp_path, monkeypatch):
    """An un-allowlisted MCP call is REFUSED at L2 (no transport touched) → the
    modality surfaces success=False. Proves execute goes THROUGH the gate."""
    monkeypatch.setenv("SYSTEMU_MCP_SERVER_URLS", "")   # no env grandfather
    from sharing_on.config import Config
    from systemu.runtime.actuation.mcp_modality import McpActuationModality
    from systemu.runtime.actuation.modality import Action

    (tmp_path / "connections").mkdir(parents=True, exist_ok=True)
    vault = _Vault(tmp_path)
    m = McpActuationModality(runtime=None, vault=vault, config=Config())
    action = Action(modality="mcp", target="https://unlisted.example.com",
                    name="create_issue", params={"title": "x"}, is_mutation=True)
    res = m.execute(action)
    assert res.success is False, "an un-allowlisted MCP call must be refused (L2)"
    assert "not enabled" in (res.error or "").lower() or "allowlist" in (res.error or "").lower()


def test_execute_l3_gate_deny_propagates(tmp_path, monkeypatch):
    """An L3 gate DENY raises PendingOperatorDecision — execute must LET IT PROPAGATE
    (swallowing it would BYPASS the gate; the loop parks+resumes it)."""
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime.actuation.mcp_modality import McpActuationModality
    from systemu.runtime.actuation.modality import Action
    import systemu.runtime.mcp.dispatch as _dispatch

    def _raise(*a, **k):
        raise PendingOperatorDecision(decision_id="d1", dedup_key="mcp:x", options=["Deny"])
    monkeypatch.setattr(_dispatch, "call_mcp_tool", _raise)

    m = McpActuationModality(runtime=None, vault=_Vault(tmp_path))
    action = Action(modality="mcp", target="https://s", name="pay", is_mutation=True)
    with pytest.raises(PendingOperatorDecision):
        m.execute(action)


# ─────────────────────────────────────────────────────────────────────────────
#  capture_evidence()
# ─────────────────────────────────────────────────────────────────────────────

def test_capture_evidence_mutation_returns_confirmed_api_readback(tmp_path):
    """A known-mutation MCP result exposing an api_readback directive (readback_url +
    fresh expected token) + an injected echo client → capture_evidence returns a
    CONFIRMED ExternalEvidence with method='api_readback' (REUSE of verify())."""
    from systemu.runtime.mcp.dispatch import _guard_mcp_output
    from systemu.runtime.actuation.mcp_modality import McpActuationModality
    from systemu.runtime.actuation.modality import Action, ActionResult

    token = "issue-42-abc"
    url = "https://api.example.com/issues/42"
    directive = {"strategy": "api_readback", "expected_tokens": [token],
                 "readback_url": url, "submit_host": "api.example.com",
                 "pre_submit_absent": True}
    guarded = _guard_mcp_output({"external": directive, "html_url": url})
    result = ActionResult(success=True, response=guarded)

    runtime = types.SimpleNamespace(_external_api_client=_EchoReadbackClient([token]))
    tool = types.SimpleNamespace(effect_tags=[], is_action_tool=True,
                                 name="mcp__srv__create_issue")
    action = Action(modality="mcp", name="mcp__srv__create_issue",
                    params={"title": "bug"}, is_mutation=True,
                    objective=_objective(), tool=tool)

    m = McpActuationModality(runtime=runtime)
    ev = m.capture_evidence(action, result)
    assert ev is not None and ev.confirmed is True, (
        f"an echoed fresh api_readback must confirm; got {ev}")
    assert ev.method == "api_readback"
    assert ev.objective_id == 7


def test_capture_evidence_read_returns_none():
    """A READ MCP call (is_mutation=False) has no external effect to verify → None."""
    from systemu.runtime.actuation.mcp_modality import McpActuationModality
    from systemu.runtime.actuation.modality import Action, ActionResult
    m = McpActuationModality(runtime=types.SimpleNamespace(_external_api_client=None))
    action = Action(modality="mcp", name="mcp__srv__list_issues",
                    is_mutation=False, objective=_objective())
    assert m.capture_evidence(action, ActionResult(success=True, response={})) is None


def test_capture_evidence_nonmoney_mutation_no_channel_returns_none():
    """A non-money mutation with NO evidence channel (no directive / no resource url) →
    None (the credit stays on today's path — byte-identical)."""
    from systemu.runtime.actuation.mcp_modality import McpActuationModality
    from systemu.runtime.actuation.modality import Action, ActionResult
    from systemu.runtime.mcp.dispatch import _guard_mcp_output
    guarded = _guard_mcp_output({"ok": True, "note": "no verification channel here"})
    m = McpActuationModality(runtime=types.SimpleNamespace(_external_api_client=None))
    action = Action(modality="mcp", name="mcp__srv__toggle_flag",
                    is_mutation=True, objective=_objective(),
                    tool=types.SimpleNamespace(effect_tags=[], is_action_tool=True))
    assert m.capture_evidence(action, ActionResult(success=True, response=guarded)) is None
