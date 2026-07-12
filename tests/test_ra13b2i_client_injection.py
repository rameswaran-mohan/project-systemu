"""R-A13b-2i TASK 2 — inject the prod client + FORCE money-move through branch-1.

Closes the self-attestation hole in _build_external_api_client: a money-move may
NEVER resolve to branch-2 (the _EnvelopeClient over the tool's OWN self-reported
readback_envelope). With NO injected independent client, a money-move has NO
admissible transport ⇒ fail closed (would-PARK). A non-money effect may still use
branch-2. And the injection is GATED on the stamp net being armed (OFF injects
nothing ⇒ byte-identical, no outbound GET).
"""
from __future__ import annotations

from types import SimpleNamespace

from systemu.runtime.shadow_runtime import _build_external_api_client
from systemu.runtime.readback_client import ProdReadbackClient

# reuse the real vault-building harness (no tests/__init__.py — sibling import).
from test_s3_credit_wiring import _build_entities_objs, _EchoReadbackClient


# ── _build_external_api_client: the branch-selection contract ──
def test_money_move_refuses_envelope_branch_when_no_client():
    """MONEY-MOVE + a tool-supplied readback_envelope + NO injected client ⇒ None
    (NOT the self-reported _EnvelopeClient) — the hole is closed, verifier fails
    closed ⇒ would-PARK."""
    rt = SimpleNamespace(_external_api_client=None)
    ev_in = {"readback_envelope": {"observed_tokens": ["forged"]}}
    client = _build_external_api_client(rt, ev_in, is_money_move=True)
    assert client is None, (
        "a money-move must NEVER fall back to the self-reported _EnvelopeClient")


def test_non_money_may_use_envelope_branch():
    """A NON-money effect keeps the already-wired branch-2 envelope fallback."""
    rt = SimpleNamespace(_external_api_client=None)
    ev_in = {"readback_envelope": {"observed_tokens": ["ok"]}}
    client = _build_external_api_client(rt, ev_in, is_money_move=False)
    assert client is not None and hasattr(client, "readback"), client
    # it echoes the tool's OWN envelope (advisory, non-money only).
    assert client.readback("ignored") == {"observed_tokens": ["ok"]}


def test_injected_independent_client_always_wins():
    """An injected independent client (branch-1) wins for BOTH money and non-money —
    it is the production reader / the test mock."""
    injected = _EchoReadbackClient(echo_tokens=["t"])
    rt = SimpleNamespace(_external_api_client=injected)
    ev_in = {"readback_envelope": {"observed_tokens": ["forged"]}}
    assert _build_external_api_client(rt, ev_in, is_money_move=True) is injected
    assert _build_external_api_client(rt, ev_in, is_money_move=False) is injected


def test_no_envelope_no_client_is_none():
    rt = SimpleNamespace(_external_api_client=None)
    assert _build_external_api_client(rt, {}, is_money_move=False) is None


# ── the __init__ injection gate (mode != off) ──
def _make_runtime(tmp_path):
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    vault, _shadow, _activity = _build_entities_objs(tmp_path, objectives=[])
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    return ShadowRuntime(cfg, vault)


def test_off_mode_client_present_but_dormant_for_non_mcp(tmp_path, monkeypatch):
    """R-A14a reframe of the old ``test_off_mode_injects_no_client``.

    Its ORIGINAL intent was "no outbound GET / no credit-gate change when the net is
    disarmed". R-A14a decoupled the MCP verification obligation from the stamp mode, so
    the readback transport is now injected UNCONDITIONALLY (a non-money MCP mutation must
    be verifiable net-OFF). The invariant is NO LONGER "client is None" — it is that the
    client is PRESENT but **DORMANT for a NON-MCP effect in OFF**: it is never read, so
    there is no outbound GET and the credit is byte-identical.

    Asserts THAT directly: OFF injects a ProdReadbackClient, and driving a non-external,
    non-MCP objective in OFF credits normally, writes NO ExternalEvidence, and issues
    ZERO readbacks through the injected client's transport."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "off")

    # inject a REAL ProdReadbackClient whose httpx transport COUNTS reads (dormancy proof).
    import httpx
    import systemu.runtime.readback_client as rc
    reads = {"n": 0}

    def _handler(request):
        reads["n"] += 1
        return httpx.Response(200, headers={"content-type": "application/json"}, json={})

    _real = rc.ProdReadbackClient
    transport = httpx.MockTransport(_handler)

    def _factory(*a, **k):
        k.setdefault("transport", transport)
        return _real(*a, **k)
    monkeypatch.setattr(rc, "ProdReadbackClient", _factory)

    # Drive a non-external, non-MCP objective in OFF. The runtime the harness builds
    # gets the injected client (via the factory above); we assert it is PRESENT + DORMANT.
    from systemu.core.models import Objective
    from test_s3_credit_wiring import _drive_live_credit
    obj = Objective(id=1, goal="write the local report file",
                    success_criteria="file exists",
                    requires_external_verification=False)
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[obj], claim_obj_id=1, tool_parsed={"ok": True})

    # PRESENT: OFF now injects the client (a real ProdReadbackClient instance).
    assert type(getattr(runtime, "_external_api_client", None)).__name__ == "ProdReadbackClient", (
        "OFF must now inject a ProdReadbackClient (present)")
    # DORMANT / byte-identical: credited unchanged, no ExternalEvidence, no outbound GET.
    assert result.get("status") == "success", (
        f"a non-external non-MCP OFF objective must credit unchanged; got {result.get('status')}")
    assert not (getattr(ctx, "_external_evidence", {}) or {}), (
        "a non-MCP OFF effect must write no ExternalEvidence")
    assert reads["n"] == 0, "the injected client must be DORMANT for a non-MCP OFF effect"


def test_shadow_mode_injects_prod_client(tmp_path, monkeypatch):
    """When the net is armed (SHADOW), the prod independent reader is injected so a
    real would-credit becomes recordable end-to-end."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    rt = _make_runtime(tmp_path)
    assert isinstance(getattr(rt, "_external_api_client", None), ProdReadbackClient), (
        "SHADOW must inject a ProdReadbackClient")


def test_enforce_mode_injects_prod_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    rt = _make_runtime(tmp_path)
    assert isinstance(getattr(rt, "_external_api_client", None), ProdReadbackClient)
