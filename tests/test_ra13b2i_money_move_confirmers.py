"""R-A13b-2i FIX 2 (C2) — a money-move may be confirmed ONLY by a hardened,
independent, host-pinned, provably-fresh api_readback.

The exploit: ``email_confirm`` is in ``_MONEY_MOVE_STRONG`` so the money-move hard
gate never demoted it, and ``_run_external_verification`` builds ONLY an api_client
(never an email_client) — so ``_email_confirm(email_client=None)`` self-confirms on
the tool's OWN inline ``observed_tokens`` vs ``expected_tokens`` (no independent
fetch, no host-pin, no freshness). A money-move tool emitting
``{"strategy":"email_confirm","expected_tokens":["X"],"observed_tokens":["X"]}``
would self-credit a money-move.

Driven through the REAL credit seam (the SHADOW would-credit/would-park meter →
``_run_external_verification`` → ``ExternalVerifier.verify``), NOT synthetic dicts.
The NON-money email_confirm no-regression is asserted through the real ``verify``.
"""
from __future__ import annotations

from test_s3_credit_wiring import _drive_live_credit
from test_ra13b1_shadow_meter import _shadow_obj, _stamp_shadow_on_resolve


# ── C2 EXPLOIT: a money-move email_confirm self-report ⇒ would-PARK (was CREDIT) ──
def test_money_move_email_confirm_inline_would_park(tmp_path, monkeypatch):
    """A would-stamp SHADOW objective (armed ⇒ a fail-closed money-move: external +
    unclassified effect) whose tool emits strategy=email_confirm with inline
    observed==expected must NOT self-confirm: the money-move gate demotes every
    channel that is not a hardened api_readback ⇒ would-PARK.

    Pre-fix this recorded would_credit=True (the exploit); this test fails then and
    passes only once the gate demotes money-move email_confirm."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "CONF-EMAIL-777"
    tool_parsed = {
        "ok": True,
        "external": {
            "strategy": "email_confirm",
            "expected_tokens": [token],
            "observed_tokens": [token],   # the TOOL self-reports its own echo
        },
    }
    # NO api_client AND NO email_client injected — mirrors the real runtime, which
    # never wires an email_client. The only "evidence" is the tool's inline echo.
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed)

    assert result.get("status") == "success"    # record-only
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("would_park") is True and ev.get("would_credit") is False, (
        "a money-move email_confirm self-report must be DEMOTED (no independent "
        f"host-pinned fresh readback) ⇒ would-PARK; ev={ev}")


# ── the legit money-move channel — a hardened api_readback — still WOULD-CREDIT ──
def test_money_move_hardened_api_readback_still_would_credit(tmp_path, monkeypatch):
    """No-regression: the ONE admissible money-move channel — a hardened
    api_readback (injected independent client + host-pin + https + a runtime
    freshness probe) — still WOULD-CREDIT. The fix only closes the non-readback
    self-report channels."""
    from test_s3_credit_wiring import _CreateOnceReadbackClient
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-hardened-1"
    url = "https://api.example.com/rows/1"
    client = _CreateOnceReadbackClient(echo_tokens=[token])
    directive = {"readback_url": url, "expected_tokens": [token],
                 "submit_host": "api.example.com"}
    tool_parsed = {"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "readback_url": url, "submit_host": "api.example.com"}}
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client,
        decision_params={"external": directive})

    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("would_credit") is True and ev.get("would_park") is False, ev


# ── NO-REGRESSION: a NON-money email_confirm still confirms (real verify path) ──
def test_non_money_email_confirm_still_confirms():
    """A NON-money external objective (a known ``send_message`` effect) whose
    confirmation email echoes the expected token still confirms via email_confirm —
    the money-move gate only demotes when the objective is a money-move. This runs
    the REAL ``ExternalVerifier.verify`` (the actual function), not a synthetic
    money-move gate call."""
    from systemu.runtime.external_verifier import ExternalVerifier
    from systemu.runtime.effect_tags import EffectTag

    class _Obj:
        def __init__(self):
            self.id = 1
            self.objective_id = 1
            self.text = "send the receipt email to the customer"
            self.params = {}
            self.effect_tags = {EffectTag.SEND_MESSAGE}
            self.requires_external = True

    class _EmailClient:
        def fetch(self, query=None):
            return {"subject": "Your confirmation CONF-XYZ",
                    "body": "Message CONF-XYZ delivered."}

    v = ExternalVerifier(email_client=_EmailClient())
    ev = v.verify(_Obj(), effect_class="send_message",
                  evidence_input={"strategy": "email_confirm",
                                  "expected_tokens": ["CONF-XYZ"],
                                  "email_query": "from:receipts@example.com"})
    assert ev.confirmed is True and ev.method == "email_confirm", ev
