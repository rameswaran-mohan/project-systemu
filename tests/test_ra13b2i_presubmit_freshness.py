"""R-A13b-2i TASK 3 — the SEAM-3 pre-submit freshness probe (real credit seam).

The shipped SHADOW meter over-reported would-PARK because the presubmit-snapshot
guard only fired on the LIVE requires_external_verification field (which SHADOW
never writes) ⇒ freshness was ALWAYS unprovable ⇒ even a correct token echo parked.
2i widens the guard to the ARMED view and makes the capture a REAL independent
pre-submit probe, and FORBIDS tool-self-carried freshness for a money-move.

Driven through the REAL credit seam (the SHADOW park-surface meter), NOT synthetic
evidence dicts.
"""
from __future__ import annotations

from test_s3_credit_wiring import _drive_live_credit, _EchoReadbackClient, _CreateOnceReadbackClient
from test_ra13b1_shadow_meter import _shadow_obj, _stamp_shadow_on_resolve, _metrics_snapshot


def _directive(token):
    return {"readback_url": "https://api.example.com/rows/1",
            "expected_tokens": [token], "submit_host": "api.example.com"}


def _envelope(token, **extra):
    ext = {"strategy": "api_readback", "expected_tokens": [token],
           "readback_url": "https://api.example.com/rows/1",
           "submit_host": "api.example.com"}
    ext.update(extra)
    return {"ok": True, "external": ext}


# ── the guard is widened + the probe proves freshness ⇒ would-CREDIT ──
def test_shadow_presubmit_probe_proves_freshness_would_credit(tmp_path, monkeypatch):
    """In SHADOW a would-stamp objective with a readback_url directive now CAPTURES a
    presubmit snapshot (guard widened to the armed view) and the INDEPENDENT probe
    finds the create-once token ABSENT pre-submit ⇒ fresh ⇒ the post-submit echo
    confirms ⇒ would-CREDIT. Record-only: the run completes normally."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-fresh-1"
    client = _CreateOnceReadbackClient(echo_tokens=[token])   # absent → present
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=_envelope(token),           # NO tool-carried pre_submit_absent
        api_client=client, decision_params={"external": _directive(token)})

    assert result.get("status") == "success"
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("shadow") is True, ev
    assert ev.get("would_credit") is True and ev.get("would_park") is False, ev
    # the independent client was read PRE-submit (probe) AND POST-submit (verify).
    assert len(client.urls) >= 2, (
        f"the probe (pre-submit) + verify (post-submit) must both read back; {client.urls}")
    snap = _metrics_snapshot(runtime)
    assert sum(v.get("would_credit", 0) for v in snap.values()) >= 1, snap


# ── a REPLAYED token (present pre-submit) ⇒ would-PARK even with a matching echo ──
def test_shadow_replayed_token_present_presubmit_would_park(tmp_path, monkeypatch):
    """The probe finds the expected token ALREADY present pre-submit (a replay / a
    pre-existing row) ⇒ STALE ⇒ the freshness gate refuses even though the
    post-submit echo matches ⇒ would-PARK."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-replay-1"
    client = _EchoReadbackClient(echo_tokens=[token])   # PRESENT pre AND post
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=_envelope(token), api_client=client,
        decision_params={"external": _directive(token)})

    assert result.get("status") == "success"     # record-only
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("would_park") is True and ev.get("would_credit") is False, (
        "a token already present pre-submit is STALE ⇒ would-PARK even on a matching echo")
    snap = _metrics_snapshot(runtime)
    assert sum(v.get("would_park", 0) for v in snap.values()) >= 1, snap
    assert sum(v.get("would_credit", 0) for v in snap.values()) == 0, snap


# ── a money-move tool self-reporting pre_submit_absent=True is NOT trusted ──
def test_money_move_self_reported_freshness_not_trusted(tmp_path, monkeypatch):
    """SECURITY: with NO runtime probe (no directive), a money-move's freshness is
    UNPROVABLE — the tool's OWN ``pre_submit_absent=True`` on the result envelope is
    IGNORED (Option-A self-attested anti-replay is forgeable) ⇒ would-PARK, even
    though the post-submit echo matches."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-selfreport-1"
    client = _EchoReadbackClient(echo_tokens=[token])
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=_envelope(token, pre_submit_absent=True),  # the tool self-attests
        api_client=client)                                     # NO decision directive ⇒ no probe

    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("would_park") is True and ev.get("would_credit") is False, (
        "a money-move must not trust tool-self-carried freshness; without a runtime "
        f"probe it is unprovable ⇒ would-PARK; ev={ev}")


# ── the decision-param self-report is ALSO not trusted for a money-move ──
def test_money_move_decision_param_self_report_not_trusted(tmp_path, monkeypatch):
    """Even a decision-param self-report (``parameters.pre_submit_absent``) — which
    the agent controls — cannot supply money-move freshness: only a REAL probe
    (``probe_ran``) is trusted. No directive ⇒ no probe ⇒ would-PARK."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-decparam-1"
    client = _EchoReadbackClient(echo_tokens=[token])
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=_envelope(token), api_client=client,
        decision_params={"pre_submit_absent": True, "presubmit_tokens": ["decoy"]})

    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("would_park") is True and ev.get("would_credit") is False, ev
