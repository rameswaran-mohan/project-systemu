"""R-A14a slice 1+2 — the DEFAULT-OFF regression + the fix (through the REAL transport).

R-A14a decoupled the MCP verification OBLIGATION from ``SYSTEMU_S4_STAMP`` (the credit
seam runs ``_mcp_actuation_link`` regardless of the stamp mode), but the readback
TRANSPORT stayed coupled: the ``ProdReadbackClient`` was injected at
``ShadowRuntime.__init__`` ONLY when ``_s4_stamp_mode() != "off"``. So in the DEFAULT
OFF config a non-money MCP create-resource mutation had NO client ⇒ the reused
``verify()`` reported "no api_client for readback" ⇒ ``confirmed=False`` ⇒ the
over-gated seam withheld the credit → a PERMANENT regression (before R-A14a an
unstamped MCP tool credited on the local verdict).

These tests drive through the **REAL** transport (a real ``ProdReadbackClient`` whose
only mock is the httpx ``MockTransport``) — NOT an injected mock client on the runtime.
That injected-mock masking is exactly what hid the bug: it set
``runtime._external_api_client`` AFTER construction, so it never exercised the OFF
injection path. Here the ShadowRuntime must inject the client ITSELF (OFF included).

The fix has two parts:
  PART 1 — inject the ``ProdReadbackClient`` UNCONDITIONALLY (dormant unless an
           obligation fires), so the decoupled MCP obligation is SATISFIABLE net-OFF.
  PART 2 — a NON-money MCP mutation's verification is a NON-GATING receipt (credit
           proceeds on the local verdict, verified/claimed receipt is best-effort
           provenance); a MONEY-MOVE MCP mutation STAYS fail-closed (gates the credit).
"""
from __future__ import annotations

import httpx

# sibling imports (no tests/__init__.py) — reuse the live-credit harness + MCP helpers.
from test_s3_credit_wiring import _drive_live_credit
from test_ra14a_mcp_credit_link import _register_v2_mcp, _mcp_result, _mcp_tool, _external_obj


# A public literal-IP host keeps the ProdReadbackClient SSRF gate hermetic (no DNS) —
# a literal IP connects direct through the injected MockTransport.
_PUB_IP = "93.184.216.34"


def _patch_prod_client_transport(monkeypatch, handler):
    """Make ShadowRuntime's OWN ``ProdReadbackClient()`` construction use an httpx
    ``MockTransport`` — a REAL ProdReadbackClient (real SSRF gate / host-pin / httpx
    stack), only the socket layer mocked. Returns the transport (a call counter)."""
    import systemu.runtime.readback_client as rc
    _real = rc.ProdReadbackClient
    transport = httpx.MockTransport(handler)

    def _factory(*a, **k):
        k.setdefault("transport", transport)
        return _real(*a, **k)

    monkeypatch.setattr(rc, "ProdReadbackClient", _factory)
    return transport


# ─────────────────────────────────────────────────────────────────────────────
#  RED → GREEN — the regression: a non-money MCP create-resource mutation whose
#  result WOULD verify (the real readback echoes the token) must CREDIT in OFF,
#  with a VERIFIED (confirmed=True) receipt persisted.
# ─────────────────────────────────────────────────────────────────────────────

def test_non_money_mcp_verifiable_credits_off_real_transport(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)   # DEFAULT OFF

    url = f"https://{_PUB_IP}/repos/o/r/issues/42"

    def _handler(request):
        # the created resource re-read: echoes the id token so verify() confirms.
        return httpx.Response(
            200, headers={"content-type": "application/json"},
            json={"id": "42", "number": 42, "html_url": url, "state": "open"})

    _patch_prod_client_transport(monkeypatch, _handler)

    name = "mcp__github__create_issue"
    cleanup = _register_v2_mcp(name, is_action_tool=True)
    try:
        # a created-resource shape (no explicit `external` — SYNTHESIZED directive):
        # a public https resource URL + an id/number token.
        tool_parsed = _mcp_result({"html_url": url, "number": 42, "id": "42"})

        runtime, result, ctx = _drive_live_credit(
            tmp_path, monkeypatch,
            objectives=[_external_obj("open a GitHub issue for the login bug")],
            claim_obj_id=1, tool_parsed=tool_parsed,
            tool=_mcp_tool(name))   # NB: NO api_client — the runtime injects it ITSELF

        assert result.get("status") == "success", (
            "REGRESSION: a non-money MCP create-resource mutation whose real readback "
            "confirms the token must CREDIT in DEFAULT OFF (before the fix: no client "
            f"injected in OFF ⇒ confirmed=False ⇒ NOT credited); got {result.get('status')}")
        store = getattr(ctx, "_external_evidence", {}) or {}
        ev = store.get("1") or store.get(1)
        assert ev and ev.get("confirmed") is True, (
            "a VERIFIED (confirmed=True) receipt must be persisted — proving the "
            f"injected-in-OFF ProdReadbackClient really read back; store={store}")
        assert ev.get("method") == "api_readback"
        assert not ev.get("shadow"), "the MCP receipt is a LIVE credit, not a shadow record"
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  Non-money OFF UNVERIFIABLE → CREDITED anyway (no stall). The receipt can't
#  confirm (404 / no token echoed), but the credit must PROCEED via the normal
#  path (PART 2: non-money MCP verification is non-gating). No regression, no stall.
# ─────────────────────────────────────────────────────────────────────────────

def test_non_money_mcp_unverifiable_still_credits_off_no_stall(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)

    url = f"https://{_PUB_IP}/repos/o/r/issues/77"

    def _handler(request):
        return httpx.Response(404, headers={"content-type": "application/json"},
                              json={"message": "Not Found"})   # cannot confirm

    _patch_prod_client_transport(monkeypatch, _handler)

    name = "mcp__github__create_comment"
    cleanup = _register_v2_mcp(name, is_action_tool=True)
    try:
        tool_parsed = _mcp_result({"html_url": url, "number": 77, "id": "77"})

        runtime, result, ctx = _drive_live_credit(
            tmp_path, monkeypatch,
            objectives=[_external_obj("comment on the tracking issue")],
            claim_obj_id=1, tool_parsed=tool_parsed, tool=_mcp_tool(name))

        assert result.get("status") == "success", (
            "a non-money MCP mutation whose readback CANNOT confirm must STILL credit "
            f"via the normal path (non-gating receipt) — no stall; got {result.get('status')}")
        store = getattr(ctx, "_external_evidence", {}) or {}
        ev = store.get("1") or store.get(1)
        # a receipt may be persisted (best-effort provenance) but must NOT be confirmed.
        assert not (ev and ev.get("confirmed") is True), (
            f"an unverifiable readback must NOT confirm the receipt; store={store}")
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  Money-move MCP OFF fail-closed (the INVARIANT) — through the REAL transport.
#  (a) inline/advisory-only signal → NOT credited.
#  (b) even a hardened readback that ECHOES the token → NOT credited, because a
#      money-move's freshness may come ONLY from an independent pre-submit probe
#      (none for MCP in slices 1+2) ⇒ the create-once proof is unprovable ⇒ the
#      hardened readback refuses. The money-move gate holds fail-closed.
# ─────────────────────────────────────────────────────────────────────────────

def test_money_move_mcp_inline_only_not_credited_real(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)

    # the readback would echo the token — but an inline money-move has NO readback_url,
    # so verify()'s money-move gate demotes it regardless.
    def _handler(request):
        return httpx.Response(200, headers={"content-type": "application/json"},
                              json={"id": "pay-1", "status": "paid"})

    _patch_prod_client_transport(monkeypatch, _handler)

    name = "mcp__pay__send_payment"
    cleanup = _register_v2_mcp(name, is_action_tool=True)
    try:
        obs = []
        tok = "pay-confirm-1"
        directive = {"strategy": "api_readback", "expected_tokens": [tok],
                     "observed_tokens": [tok], "pre_submit_absent": True}  # INLINE, no readback_url
        tool_parsed = _mcp_result({"external": directive})

        runtime, result, ctx = _drive_live_credit(
            tmp_path, monkeypatch,
            objectives=[_external_obj("pay the $500 invoice via the payments API")],
            claim_obj_id=1, tool_parsed=tool_parsed, spy_obs=obs, tool=_mcp_tool(name))

        assert result.get("status") != "success", (
            "a money-move MCP with only an inline signal must NOT credit; "
            f"got {result.get('status')}")
        unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
        assert unv, f"expected an UNVERIFIED_EXTERNAL observation; saw {obs}"
        store = getattr(ctx, "_external_evidence", {}) or {}
        ev = store.get("1") or store.get(1)
        assert not (ev and ev.get("confirmed") is True), store
    finally:
        cleanup()


def test_money_move_mcp_hardened_readback_no_probe_fail_closed_real(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)

    url = f"https://{_PUB_IP}/payments/tx-42"
    tok = "tx-42-receipt"

    def _handler(request):
        # the hardened readback DOES echo the token — the ONLY thing missing is a
        # fresh independent pre-submit probe (none for MCP in slices 1+2).
        return httpx.Response(200, headers={"content-type": "application/json"},
                              json={"id": tok, "status": "settled"})

    _patch_prod_client_transport(monkeypatch, _handler)

    name = "mcp__pay__wire_funds"
    cleanup = _register_v2_mcp(name, is_action_tool=True)
    try:
        obs = []
        # a HARDENED directive: readback_url + submit_host + a self-reported freshness
        # claim. For a money-move the reused engine ZEROES the self-reported freshness
        # (only an independent probe is trusted) ⇒ the create-once proof is unprovable.
        directive = {"strategy": "api_readback", "expected_tokens": [tok],
                     "readback_url": url, "submit_host": _PUB_IP,
                     "pre_submit_absent": True}
        tool_parsed = _mcp_result({"external": directive})

        runtime, result, ctx = _drive_live_credit(
            tmp_path, monkeypatch,
            objectives=[_external_obj("wire $2000 to the vendor account")],
            claim_obj_id=1, tool_parsed=tool_parsed, spy_obs=obs, tool=_mcp_tool(name))

        assert result.get("status") != "success", (
            "a money-move MCP with a hardened readback but NO independent fresh probe "
            "must STILL fail closed (freshness unprovable — the create-once proof is "
            f"the anti-replay bar); got {result.get('status')}")
        store = getattr(ctx, "_external_evidence", {}) or {}
        ev = store.get("1") or store.get(1)
        assert not (ev and ev.get("confirmed") is True), (
            f"a money-move must never confirm without a fresh independent probe; store={store}")
    finally:
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  Non-MCP OFF byte-identical — the injected-but-dormant client changes NOTHING.
#  A non-external, non-MCP objective in OFF credits on the local verdict, and the
#  injected ProdReadbackClient is NEVER read (0 outbound reads).
# ─────────────────────────────────────────────────────────────────────────────

def test_non_mcp_off_byte_identical_client_dormant(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_S4_STAMP", raising=False)

    reads = {"n": 0}

    def _handler(request):
        reads["n"] += 1
        return httpx.Response(200, headers={"content-type": "application/json"}, json={})

    _patch_prod_client_transport(monkeypatch, _handler)

    from systemu.core.models import Objective
    obj = Objective(id=1, goal="write the local report file",
                    success_criteria="file exists",
                    requires_external_verification=False)   # non-external, non-MCP

    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[obj], claim_obj_id=1,
        tool_parsed={"ok": True})   # a plain (non-MCP) tool result

    assert result.get("status") == "success", (
        f"a non-external non-MCP objective must credit unchanged; got {result.get('status')}")
    store = getattr(ctx, "_external_evidence", {}) or {}
    assert not store, f"no ExternalEvidence must be written for a non-MCP OFF objective; {store}"
    # the injected client must be PRESENT (PART 1) but DORMANT (no outbound read).
    # NB: ``ProdReadbackClient`` is monkeypatched to a factory here, so assert on the
    # concrete class NAME of the injected instance (it IS a real ProdReadbackClient).
    _client = getattr(runtime, "_external_api_client", None)
    assert type(_client).__name__ == "ProdReadbackClient", (
        f"OFF must now inject a ProdReadbackClient (present, dormant); got {_client!r}")
    assert reads["n"] == 0, "the injected client must be DORMANT for a non-MCP OFF effect"
