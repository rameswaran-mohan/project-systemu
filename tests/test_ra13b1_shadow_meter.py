"""R-A13b-1 — the SHADOW park-surface METER (record-only) LIVE-CONSUMER AC.

The anti-dormancy tripwire (design §"LIVE-CONSUMER AC"): through the REAL execute
loop + a REAL schema-bearing Tool at the credit seam, with SYSTEMU_S4_STAMP=shadow
and a would-stamp objective whose LIVE field is NOT written (only ``_s4_stamp_shadow``):

  (i)  a tool result carrying a valid parsed['external'] envelope + an INJECTED MOCK
       readback client that confirms → the meter RECORDS would-credit (metrics bucket
       incremented + a shadow=True would_credit entry in context._external_evidence),
       and the run still completes normally (credited via the local-verifier path;
       no card, no park).
  (ii) the same tool with NO envelope → S3 fail-closed → the meter records would-park.

Record-only: the meter NEVER changes _do_credit / enqueues a card / suspends. OFF and
ENFORCE runs never enter the meter (additive / byte-identical).

Driven through the REAL credit-seam path (NOT synthetic evidence dicts) — reuses the
drive-execute harness from tests/test_s3_credit_wiring.py.
"""
from __future__ import annotations

from test_s3_credit_wiring import (  # the REAL drive-execute harness (no tests/__init__.py)
    _drive_live_credit,
    _EchoReadbackClient,
)


def _shadow_obj(**overrides):
    """A would-stamp objective in SHADOW: the LIVE gate field is False (SHADOW never
    writes it). The binder-produced ``_s4_stamp_shadow`` attr is injected onto the
    LOOP's objective instances by ``_stamp_shadow_on_resolve`` (below) — objectives are
    reloaded from the vault mid-run, which strips this non-field attr, so it must be set
    on the instances the loop actually uses (exactly where the binder sets it)."""
    from systemu.core.models import Objective
    base = dict(id=1, goal="POST the row to the external API",
                success_criteria="row visible via readback",
                requires_external_verification=False)   # SHADOW: live field NOT written
    base.update(overrides)
    return Objective(**base)


def _stamp_shadow_on_resolve(monkeypatch):
    """Stamp ``_s4_stamp_shadow=True`` onto the objectives the execute loop actually
    runs — mirrors requirement_binder.py:648 (objective.__dict__["_s4_stamp_shadow"] =
    stamp), which the harness bypasses (it stubs _handle_tool_call). Patched BEFORE
    _drive_live_credit so its internal _resolve spy chains onto this wrapper."""
    import systemu.runtime.shadow_runtime as _sr
    orig = _sr._resolve_objectives_for_run

    def _wrap(**kw):
        objs, sj = orig(**kw)
        for o in objs:
            try:
                o.__dict__["_s4_stamp_shadow"] = True
            except Exception:
                pass
        return objs, sj
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _wrap)


def _metrics_snapshot(runtime):
    from systemu.runtime.metrics_store import MetricsStore
    from pathlib import Path
    return MetricsStore(Path(runtime.vault.root) / "metrics").shadow_meter_snapshot()


# ── (i) envelope + confirming mock client → WOULD-CREDIT, run completes normally ──
def test_shadow_meter_records_would_credit(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-shadow-777"
    client = _EchoReadbackClient(echo_tokens=[token])
    tool_parsed = {
        "ok": True,
        "external": {
            "strategy": "api_readback",
            "expected_tokens": [token],
            "submission_token": token,
            "readback_url": "https://api.example.com/rows/777",
            "submit_host": "api.example.com",
            "pre_submit_absent": True,   # freshness proof on the envelope
        },
    }
    obs = []
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client, spy_obs=obs)

    # the run completes normally — credited via the NORMAL local-verifier path
    # (the meter did NOT change the credit outcome).
    assert result.get("status") == "success", (
        f"the shadow meter must not change the run outcome; got {result.get('status')}")
    # the mock readback client WAS driven through the real S3 chain at the seam.
    assert client.urls == ["https://api.example.com/rows/777"], (
        f"the meter must run _run_external_verification via the injected client; saw {client.urls}")
    # RECORD 1: a shadow=True would_credit entry in the run-local evidence store.
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev, f"the meter must persist a run-local shadow evidence entry; store={store}"
    assert ev.get("shadow") is True, f"the entry must be tagged shadow=True; ev={ev}"
    assert ev.get("would_credit") is True and ev.get("would_park") is False, ev
    assert ev.get("confirmed") is True, ev
    # RECORD 2: the cross-run metrics bucket incremented on the would_credit side.
    snap = _metrics_snapshot(runtime)
    total = {k: (v.get("would_credit", 0), v.get("would_park", 0)) for k, v in snap.items()}
    assert any(c >= 1 for c, _ in total.values()), (
        f"metrics must record a would_credit for the shadow meter; snap={snap}")
    assert sum(p for _, p in total.values()) == 0, f"no would_park expected; snap={snap}"
    # record-only: no operator card / no UNVERIFIED_EXTERNAL park signal was emitted.
    assert not [o for o in obs if isinstance(o, dict)
                and o.get("type") == "UNVERIFIED_EXTERNAL"], obs


# ── (ii) no envelope → S3 fail-closed → WOULD-PARK, run still completes ──
def test_shadow_meter_records_would_park(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    obs = []
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed={"ok": True},   # NO external envelope ⇒ S3 fails closed
        spy_obs=obs)

    # the run still completes normally (record-only — the local verifier credits).
    assert result.get("status") == "success", (
        f"the shadow meter must not park the run; got {result.get('status')}")
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev, f"the meter must persist a shadow evidence entry even on fail-closed; store={store}"
    assert ev.get("shadow") is True and ev.get("would_park") is True, ev
    assert ev.get("would_credit") is False and ev.get("confirmed") is not True, ev
    snap = _metrics_snapshot(runtime)
    parks = sum(v.get("would_park", 0) for v in snap.values())
    credits = sum(v.get("would_credit", 0) for v in snap.values())
    assert parks >= 1 and credits == 0, f"metrics must record a would_park; snap={snap}"


# ── the measurable distinction — same objective, credit vs park purely on evidence ──
def test_shadow_meter_distinction_is_measurable(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-dist-1"
    client = _EchoReadbackClient(echo_tokens=[token])
    good = {"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "readback_url": "https://api.example.com/rows/1", "submit_host": "api.example.com",
        "pre_submit_absent": True}}
    rt_c, _, ctx_c = _drive_live_credit(
        tmp_path / "credit", monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=good, api_client=client)
    rt_p, _, ctx_p = _drive_live_credit(
        tmp_path / "park", monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed={"ok": True})
    ev_c = (getattr(ctx_c, "_external_evidence", {}) or {}).get("1")
    ev_p = (getattr(ctx_p, "_external_evidence", {}) or {}).get("1")
    assert ev_c.get("would_credit") is True and ev_p.get("would_park") is True, (ev_c, ev_p)
    # the two identical objectives are distinguished ONLY by the produced evidence.
    assert ev_c.get("would_credit") != ev_p.get("would_credit")


# ── ADDITIVE AC: OFF and ENFORCE never enter the meter (byte-identical) ──
def test_off_run_never_enters_the_meter(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "off")
    # even with a stray _s4_stamp_shadow attr, OFF must not fire the meter.
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed={"ok": True})
    assert result.get("status") == "success"
    assert not (getattr(ctx, "_external_evidence", {}) or {}), (
        "OFF must write NO shadow evidence (byte-identical to today)")
    assert _metrics_snapshot(runtime) == {}, "OFF must record no meter counters"


# ── FIX A: a shadow-meter would-credit must NOT leak into the operator committed-
#    effects ledger (symmetric with _read_external_ok's shadow refusal) ──
def test_shadow_would_credit_does_not_leak_into_committed_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token = "sub-ledger-1"
    client = _EchoReadbackClient(echo_tokens=[token])
    tool_parsed = {"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "readback_url": "https://api.example.com/rows/1", "submit_host": "api.example.com",
        "pre_submit_absent": True}}
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client)

    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    # the meter DID record a shadow=True would-credit measurement …
    assert ev and ev.get("shadow") is True and ev.get("would_credit") is True, ev
    assert ev.get("confirmed") is True, ev
    # … and the metrics measurement is PRESERVED (the report still counts it) …
    snap = _metrics_snapshot(runtime)
    assert sum(v.get("would_credit", 0) for v in snap.values()) >= 1, snap
    # … but it MUST NOT surface in the operator committed-effects ledger.
    from systemu.runtime.committed_effects import (
        render_committed_effects, committed_effect_details)
    assert committed_effect_details(store) == [], committed_effect_details(store)
    assert render_committed_effects(store) == ""
    # the finalize seam appends nothing for a shadow-only store (no leak into a
    # handoff/terminal final_summary).
    from systemu.runtime.shadow_runtime import _augment_summary_with_committed_effects
    base = "Run complete."
    assert _augment_summary_with_committed_effects(base, ctx) == base


# ── FIX B: measure the ARMED net — the money-move demotion is conditioned on
#    requires_external_verification, so the meter must reflect the would-stamp onto
#    that live field (via a NON-mutating copy) or it under-counts the park surface ──
def test_fix_b_advisory_money_move_diverges_armed_vs_unarmed(tmp_path):
    """GROUNDING PIN — the money-move demotion (ExternalVerifier._is_money_move →
    money_move_net_applies' fail-closed disjunct for an UNKNOWN effect) is
    conditioned on requires_external_verification. So the SAME advisory envelope on
    the SAME UNKNOWN-effect objective diverges:
      * requires_external=False (the PRE-FIX meter) → advisory CONFIRMS → would-CREDIT
      * requires_external=True  (the ENFORCE-faithful armed copy) → demoted → would-PARK
    The armed copy makes the meter measure the net ENFORCE would run, and NEVER
    mutates the real objective (record-only)."""
    from systemu.runtime.shadow_runtime import (
        _run_external_verification, _armed_meter_objective)
    from systemu.core.models import Objective
    from systemu.runtime.tool_sandbox import ToolResult
    from types import SimpleNamespace

    # UNKNOWN effect (no effect_tags), NO financial signal in the goal text.
    obj = Objective(id=1, goal="POST the row to the external API",
                    success_criteria="row visible", requires_external_verification=False)
    obj.__dict__["_s4_stamp_shadow"] = True
    result = ToolResult(success=True, parsed={
        "ok": True, "external": {"strategy": "operator_attest", "attested": True}})
    decision = {"tool_name": "api_tool", "parameters": {}}
    tool = SimpleNamespace(name="api_tool", effect_tags=[])
    rt = SimpleNamespace(_external_api_client=None)
    presub = {"presubmit_tokens": [], "pre_submit_absent": False}

    # PRE-FIX: the meter passed the objective UNCHANGED (requires_external=False) →
    # a WEAKER net → an advisory strategy spuriously confirms an unclassified effect.
    ev_unarmed = _run_external_verification(
        rt, objective=obj, decision=decision, tool=tool, result=result, presubmit=presub)
    assert ev_unarmed.confirmed is True, (
        "regression pin: the un-armed meter runs a weaker net → spurious would-CREDIT")

    # WITH FIX: the armed copy reflects the would-stamp onto the live field.
    armed = _armed_meter_objective(obj)
    assert obj.requires_external_verification is False, "must NOT mutate the real objective"
    assert armed.requires_external_verification is True, "the copy must be armed"
    assert armed.id == obj.id and armed is not obj
    ev_armed = _run_external_verification(
        rt, objective=armed, decision=decision, tool=tool, result=result, presubmit=presub)
    assert ev_armed.confirmed is False, (
        "the armed meter runs the SAME net ENFORCE would: the money-move gate "
        "demotes the advisory strategy → would-PARK")


def test_fix_b_strong_api_readback_still_credits_when_armed(tmp_path):
    """FIX B no-regression — the STRONG api_readback strategy (in _MONEY_MOVE_STRONG,
    with a hardened readback_url) is NEVER demoted, so an armed money-move objective
    still WOULD-CREDIT on a fresh host-pinned echo. The fix only flips ADVISORY
    strategies from a spurious credit to a park; it never suppresses a real credit."""
    from systemu.runtime.shadow_runtime import (
        _run_external_verification, _armed_meter_objective)
    from systemu.core.models import Objective
    from systemu.runtime.tool_sandbox import ToolResult
    from types import SimpleNamespace

    token = "sub-strong-1"
    obj = Objective(id=1, goal="POST the row to the external API",
                    success_criteria="row visible", requires_external_verification=False)
    obj.__dict__["_s4_stamp_shadow"] = True
    result = ToolResult(success=True, parsed={"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "readback_url": "https://api.example.com/rows/1", "submit_host": "api.example.com",
        "pre_submit_absent": True}})
    decision = {"tool_name": "api_tool", "parameters": {}}
    tool = SimpleNamespace(name="api_tool", effect_tags=[])
    rt = SimpleNamespace(_external_api_client=_EchoReadbackClient(echo_tokens=[token]))
    presub = {"presubmit_tokens": [], "pre_submit_absent": False}

    armed = _armed_meter_objective(obj)
    ev = _run_external_verification(
        rt, objective=armed, decision=decision, tool=tool, result=result, presubmit=presub)
    assert ev.confirmed is True, "a strong hardened api_readback is never demoted (would-CREDIT)"


def test_shadow_meter_advisory_money_move_would_parks(tmp_path, monkeypatch):
    """FIX B end-to-end — through the REAL credit seam, an ADVISORY envelope
    (operator_attest) on an UNKNOWN-effect would-stamp SHADOW objective now records
    would-PARK (the armed meter demotes the advisory strategy exactly as ENFORCE
    would), NOT the spurious would-credit the un-armed meter recorded. Record-only:
    the run still completes (credited via the local-verifier path)."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    tool_parsed = {"ok": True, "external": {"strategy": "operator_attest", "attested": True}}
    obs = []
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, spy_obs=obs)
    assert result.get("status") == "success", (
        f"record-only: the meter must not park the run; got {result.get('status')}")
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert ev and ev.get("shadow") is True, ev
    assert ev.get("would_park") is True and ev.get("would_credit") is False, ev
    assert ev.get("confirmed") is not True, ev
    snap = _metrics_snapshot(runtime)
    assert sum(v.get("would_park", 0) for v in snap.values()) >= 1, snap
    assert sum(v.get("would_credit", 0) for v in snap.values()) == 0, snap


def test_enforce_run_never_enters_the_shadow_meter(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    # ENFORCE writes the LIVE field → _needs_external True → the LIVE S4 path runs,
    # NOT the shadow meter. A non-external objective (live False) also never fires it.
    from systemu.core.models import Objective
    obj = Objective(id=1, goal="write the local report", success_criteria="file exists",
                    requires_external_verification=False)
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[obj], claim_obj_id=1, tool_parsed={"ok": True})
    assert result.get("status") == "success"
    assert not (getattr(ctx, "_external_evidence", {}) or {}), (
        "a non-external ENFORCE objective must write no shadow evidence")
    assert _metrics_snapshot(runtime) == {}, "ENFORCE must record no shadow-meter counters"
