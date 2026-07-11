"""R-A13b-2i TASK 4+5 — the LIVE-CONSUMER AC (anti-dormancy tripwire).

Through the REAL credit seam (the SHADOW park-surface meter), a SCHEMA-BEARING
effectful FIXTURE tool that emits a valid api_readback DIRECTIVE envelope + an
INJECTED create-once readback client (whose body echoes the expected token) with the
runtime pre-submit probe proving freshness ⇒ the meter records a REAL would-CREDIT
(metrics s4_shadow.would_credit++ + a shadow=True would_credit entry) and the run
completes normally (record-only). The SAME tool with NO envelope ⇒ would-PARK.

If this can't pass the plumbing is dormant — assert the measurable distinction through
the REAL path (NOT synthetic evidence dicts — the R-A12c/R-P1 blind spot).
"""
from __future__ import annotations

from test_s3_credit_wiring import _drive_live_credit, _CreateOnceReadbackClient
from test_ra13b1_shadow_meter import _shadow_obj, _stamp_shadow_on_resolve, _metrics_snapshot


# ── TASK 4 — the schema-bearing, effect-tagged, directive-emitting fixture tool ──
def _fixture_effectful_tool():
    """A REAL Tool entity (not a synthetic dict): a declared parameters_schema + an
    external_verification_channel. effect_tags is EMPTY (UNKNOWN/unclassified) — the
    honest state of every seed tool today, which under the ARMED net is money-move
    (the fail-closed fallback), so the hardened api_readback + the pre-submit probe
    are BOTH load-bearing. (Curated money-move/send_message tags are 2ii; real-seed
    per-API instrumentation is deferred.)"""
    from systemu.core.models import Tool, ToolType, ToolStatus
    return Tool(
        id="tool_s3w", name="api_tool",
        description=("POST a row to the external API and emit an api_readback "
                     "DIRECTIVE envelope for independent external verification."),
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED, enabled=True,
        implementation_path="vault/tools/implementations/api_tool.py",
        parameters_schema={
            "type": "object",
            "properties": {
                "row": {"type": "string", "description": "the row payload to POST"},
                "external": {"type": "object",
                             "description": "the pre-submit readback DIRECTIVE"},
            },
            "required": ["row"],
        },
        effect_tags=[],                                # UNKNOWN ⇒ armed money-move
        external_verification_channel="api_readback",
    )


def _directive(url, token):
    return {"readback_url": url, "expected_tokens": [token],
            "submit_host": "api.example.com"}


def _envelope(url, token):
    return {"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "readback_url": url, "submit_host": "api.example.com"}}


def test_fixture_tool_is_schema_bearing_and_effectful():
    """TASK 4 — the fixture is a genuine schema-bearing effectful tool entity."""
    tool = _fixture_effectful_tool()
    assert isinstance(tool.parameters_schema, dict) and tool.parameters_schema.get("required")
    assert tool.external_verification_channel == "api_readback"
    assert tool.status.value == "deployed" and tool.enabled is True


# ── TASK 5 — the anti-dormancy AC: real would-CREDIT through the real seam ──
def test_live_consumer_records_real_would_credit(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    url, token = "https://api.example.com/rows/7", "sub-ac-credit-7"
    client = _CreateOnceReadbackClient(echo_tokens=[token])   # absent → echo
    obs = []
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=_envelope(url, token), api_client=client, spy_obs=obs,
        # supply the tool's DECLARED required param (row) so the R-A13a mid-loop
        # binder binds it (source #0) and raises NO scope card — the AC exercises the
        # credit seam, not requirement resolution — PLUS the pre-submit directive.
        decision_params={"row": "payload-7", "external": _directive(url, token)},
        tool=_fixture_effectful_tool())

    # record-only: the run completes normally (credited via the local-verifier path).
    assert result.get("status") == "success", (
        f"record-only: the meter must not change the run outcome; got {result.get('status')}")
    # the injected client was driven through the REAL seam: probe (pre) + verify (post).
    assert client.urls == [url, url], client.urls
    # RECORD 1: a shadow=True would_credit entry in the run-local evidence store.
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("shadow") is True, ev
    assert ev.get("would_credit") is True and ev.get("would_park") is False, ev
    assert ev.get("confirmed") is True, ev
    # RECORD 2: the cross-run metrics bucket incremented on the would_credit side.
    snap = _metrics_snapshot(runtime)
    assert sum(v.get("would_credit", 0) for v in snap.values()) >= 1, snap
    assert sum(v.get("would_park", 0) for v in snap.values()) == 0, snap
    # record-only: NO operator park signal was emitted.
    assert not [o for o in obs if isinstance(o, dict)
                and o.get("type") == "UNVERIFIED_EXTERNAL"], obs


def test_live_consumer_no_envelope_records_would_park(tmp_path, monkeypatch):
    """The SAME fixture tool with NO directive envelope ⇒ S3 fails closed ⇒ the meter
    records would-PARK (the measurable distinction is the produced evidence alone)."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed={"ok": True},                     # NO external envelope
        decision_params={"row": "payload-x"},         # bind the required leaf (no scope card)
        tool=_fixture_effectful_tool())

    assert result.get("status") == "success"
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("shadow") is True and ev.get("would_park") is True, ev
    assert ev.get("would_credit") is False, ev
    snap = _metrics_snapshot(runtime)
    assert sum(v.get("would_park", 0) for v in snap.values()) >= 1, snap
    assert sum(v.get("would_credit", 0) for v in snap.values()) == 0, snap
