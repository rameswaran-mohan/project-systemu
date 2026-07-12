"""R-A10 step B10 — wire ``RequirementReport.ask_bundle`` → the elicitation rail.

The binder's PRODUCER (``build_requirement_report``) already exists but is never
called at runtime; B10 is its consumer + the rendering:

  * ``requirement_to_field(req)``            — one Requirement → one elicitation field
  * ``surface_ask_bundle_requirement(req)``  — render ONE requirement through the
    park/ask/resume rail (``resolve_structured_input``), single-card only
  * a producer wiring in shadow_runtime that invokes ``build_requirement_report``
    and stashes ``context._requirement_report`` (B6 persists it), FAIL-SAFE +
    AC6-safe (empty ask_bundle = no-op, no elicitation surfaced).

These are FAST unit tests: they exercise the rendering + the producer helper
DIRECTLY — they NEVER drive the full ``execute()`` loop (the R-A9 survey stage
adds a ~20s wait_for). Batched multi-requirement scope card + re-plan-on-resume
is deferred to R-A12; B10 surfaces the FIRST requirement only.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch


# ─────────────────────────────────────────────────────────────────────────────
# builders
# ─────────────────────────────────────────────────────────────────────────────

def _req(**kw):
    from systemu.core.models import Requirement
    base = dict(
        kind="input", schema_path="output/path", state="missing",
        source="schema", value_origin=None, bound_value_ref=None,
        confidence=0.0, rationale="test requirement",
    )
    base.update(kw)
    return Requirement(**base)


# ═════════════════════════════════════════════════════════════════════════════
# Part 1 — the rendering: requirement_to_field
# ═════════════════════════════════════════════════════════════════════════════

def test_credential_requirement_is_secret_url_mode():
    """Test 1: a credential requirement → a field is_secret_field routes URL-mode,
    and NO plaintext secret ever lands in ``default`` (bound_value_ref is a
    reference, never the raw value)."""
    from systemu.runtime.elicitation import requirement_to_field, is_secret_field
    req = _req(kind="credential", schema_path="auth/api_key", state="resolvable",
               source="situation", value_origin="operator",
               bound_value_ref="credential:openai", confidence=0.85,
               rationale="bound from situation")
    field = requirement_to_field(req)
    assert field["name"] == "api_key"                     # leaf of schema_path
    assert is_secret_field(field) is True                 # → URL-mode
    # NEVER pre-fill a secret's default with the (reference) bound value.
    assert "default" not in field or field.get("default") in (None, "")
    assert "credential:openai" not in str(field.get("default"))


def test_input_requirement_prefills_bound_value_as_default():
    """Test 2: a non-secret ``input`` requirement with a bound_value_ref →
    a form field whose ``default`` is the (non-secret) bound value."""
    from systemu.runtime.elicitation import requirement_to_field, is_secret_field
    req = _req(kind="input", schema_path="output/report_path", state="resolvable",
               source="situation", value_origin="operator",
               bound_value_ref="file:/tmp/report.md", confidence=0.9,
               rationale="bound from a granted-root file")
    field = requirement_to_field(req)
    assert field["name"] == "report_path"
    assert is_secret_field(field) is False                # not a secret → form field
    assert field.get("default") == "file:/tmp/report.md"  # bound value pre-filled
    assert field.get("description") == "bound from a granted-root file"


def test_decision_requirement_renders_a_field():
    """Test 3: a ``decision`` requirement renders a sensible (string) field
    carrying its rationale — no crash, a usable leaf name."""
    from systemu.runtime.elicitation import requirement_to_field
    req = _req(kind="decision", schema_path="target/account_id", state="missing",
               rationale="which account should act")
    field = requirement_to_field(req)
    assert field["name"] == "account_id"
    assert field["type"] == "string"
    assert field["description"] == "which account should act"


def test_missing_input_has_no_default():
    """A missing requirement (no bound value) → a field with NO default (nothing
    to pre-fill)."""
    from systemu.runtime.elicitation import requirement_to_field
    field = requirement_to_field(_req(kind="input", schema_path="a/b/leaf",
                                      state="missing", bound_value_ref=None))
    assert field["name"] == "leaf"
    assert "default" not in field or field.get("default") in (None, "")


# ═════════════════════════════════════════════════════════════════════════════
# Part 1 (cont) — surface_ask_bundle_requirement (single-card rail)
# ═════════════════════════════════════════════════════════════════════════════

def test_surface_builds_one_field_schema_and_carries_rationale(monkeypatch):
    """Test 4a: surface_ask_bundle_requirement builds a ONE-field requested_schema
    and calls resolve_structured_input with that schema + a message carrying the
    rationale."""
    from systemu.runtime import elicitation
    captured = {}

    def _fake_rsi(*, message, requested_schema, vault=None, config=None):
        captured["message"] = message
        captured["schema"] = requested_schema
        return {"action": "accept", "content": {"report_path": "/tmp/r.md"}}

    monkeypatch.setattr(elicitation, "resolve_structured_input", _fake_rsi)
    req = _req(kind="input", schema_path="output/report_path", state="missing",
               rationale="need the output path")
    out = elicitation.surface_ask_bundle_requirement(req, vault=None, config=None)
    assert out == {"action": "accept", "content": {"report_path": "/tmp/r.md"}}
    # a SINGLE-field schema (one card, one requirement — batching is R-A12)
    props = captured["schema"]["properties"]
    assert list(props.keys()) == ["report_path"]
    # the message carries the rationale so the operator knows WHY.
    assert "need the output path" in captured["message"]


def test_surface_headless_returns_cancel_no_hang(monkeypatch):
    """Test 4b: a headless / no-queue path returns cancel (fail-closed, never
    hangs) — request_choice returns None when there is no operator queue."""
    from systemu.runtime import elicitation
    monkeypatch.setattr(
        "systemu.interface.notifications.request_choice", lambda *a, **k: None
    )
    out = elicitation.surface_ask_bundle_requirement(
        _req(kind="input", schema_path="a/b", state="missing"),
        vault=None, config=None,
    )
    assert out == {"action": "cancel", "content": {}}


def test_surface_does_not_swallow_pending_choice(monkeypatch):
    """Test 6: surface_ask_bundle_requirement must NOT catch PendingChoiceRequest
    — the suspend IS the rail; it propagates to the resume-aware spine."""
    from systemu.runtime import elicitation
    from systemu.approval.exceptions import PendingChoiceRequest

    def _raise_pending(*a, **k):
        raise PendingChoiceRequest(decision_id="dec_x", dedup_key="elicit:x",
                                   options=["Submit"])

    monkeypatch.setattr(
        "systemu.interface.notifications.request_choice", _raise_pending
    )
    with pytest.raises(PendingChoiceRequest):
        elicitation.surface_ask_bundle_requirement(
            _req(kind="input", schema_path="a/b", state="missing"),
            vault=None, config=None,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Part 2 — the producer wiring (populate context._requirement_report)
# ═════════════════════════════════════════════════════════════════════════════

class _Ctx:
    """A minimal ctx double the binder tolerates: files_produced only; vault /
    _granted_roots absent → source #1 no-ops (fully defensive)."""
    def __init__(self):
        self.files_produced = []


def _objectives():
    from systemu.core.models import Objective
    return [Objective(id=1, goal="do it", success_criteria="Done")]


def _capability_with_missing_leaf():
    """A flat v1-style Tool.parameters_schema with a required leaf (no default)
    that NO bind source can close → yields an ask_bundle requirement."""
    return {"unbindable_param": {"type": "string", "description": "a gap"}}


def test_producer_populates_requirement_report_with_ask_bundle():
    """Test 5a: the producer wiring invoked with objectives + a capability + a
    situation that yields ask_bundle requirements → context._requirement_report
    is a dict with a NON-EMPTY ask_bundle. Calls the producer helper DIRECTLY
    (no full execute())."""
    from systemu.runtime.shadow_runtime import _populate_requirement_report

    ctx = _Ctx()
    # Mock the surface step (single-card elicitation) so this test isolates the
    # report-POPULATION assertion — the report is stashed BEFORE the surface call.
    # Without the mock, surface_ask_bundle_requirement reaches notifications.request_choice
    # whose behaviour depends on global queue state left by other tests (order-dependent
    # PendingChoiceRequest in the full suite). Mirrors tests 5b/5c.
    with patch(
        "systemu.runtime.elicitation.surface_ask_bundle_requirement",
        return_value={"action": "cancel", "content": {}},
    ):
        _populate_requirement_report(
            ctx, objectives=_objectives(),
            capability=_capability_with_missing_leaf(),
            situation={},
        )
    report = getattr(ctx, "_requirement_report", None)
    assert isinstance(report, dict)
    assert report.get("ask_bundle"), "a missing required leaf must feed ask_bundle"
    # the first ask names the gap leaf.
    first = report["ask_bundle"][0]
    assert first["schema_path"].endswith("unbindable_param")
    assert first["state"] == "missing"


def test_producer_empty_ask_is_noop_ac6_safe():
    """Test 5b: a capability with NO missing requirements (empty ask_bundle) must
    NOT surface any elicitation and must NOT perturb the snapshot — AC6-safe
    no-op. A schema whose only leaf carries a default binds silently (state=have),
    so the producer leaves ``_requirement_report`` UNSET (capture persists None,
    byte-identical to a run without a producer)."""
    from systemu.runtime.shadow_runtime import _populate_requirement_report

    ctx = _Ctx()
    with patch(
        "systemu.runtime.elicitation.surface_ask_bundle_requirement"
    ) as _surface:
        _populate_requirement_report(
            ctx, objectives=_objectives(),
            capability={"opt": {"type": "string", "default": "x"}},
            situation={},
        )
    # AC6: no gap ⇒ no report stashed (snapshot unperturbed) + no elicitation.
    assert getattr(ctx, "_requirement_report", None) is None
    _surface.assert_not_called()


def test_producer_surfaces_first_requirement_when_ask_nonempty():
    """The producer surfaces the FIRST ask_bundle requirement (single-card; the
    batched scope card is R-A12) via surface_ask_bundle_requirement."""
    from systemu.runtime.shadow_runtime import _populate_requirement_report

    ctx = _Ctx()
    with patch(
        "systemu.runtime.elicitation.surface_ask_bundle_requirement",
        return_value={"action": "cancel", "content": {}},
    ) as _surface:
        _populate_requirement_report(
            ctx, objectives=_objectives(),
            capability=_capability_with_missing_leaf(),
            situation={},
        )
    _surface.assert_called_once()


def test_producer_is_fail_safe_on_binder_error(monkeypatch):
    """FAIL-SAFE: any binder/render error → log + proceed, never crash the run
    (like R-A9's survey stage). A raising build_requirement_report leaves ctx
    unperturbed (no report, no crash)."""
    import systemu.runtime.shadow_runtime as sr

    def _boom(*a, **k):
        raise RuntimeError("binder blew up")

    monkeypatch.setattr(
        "systemu.runtime.requirement_binder.build_requirement_report", _boom
    )
    ctx = _Ctx()
    # must NOT raise
    sr._populate_requirement_report(
        ctx, objectives=_objectives(),
        capability=_capability_with_missing_leaf(), situation={},
    )
    # no report set, no crash — the run proceeds exactly as today.
    assert getattr(ctx, "_requirement_report", None) is None


def test_producer_no_capability_is_noop():
    """A None / unresolvable capability → the binder returns an empty report; the
    producer must not surface anything and must not crash (fail-safe no-op)."""
    from systemu.runtime.shadow_runtime import _populate_requirement_report

    ctx = _Ctx()
    with patch(
        "systemu.runtime.elicitation.surface_ask_bundle_requirement"
    ) as _surface:
        _populate_requirement_report(
            ctx, objectives=_objectives(), capability=None, situation={},
        )
    _surface.assert_not_called()
    # an empty report is still a dict (the round-trip stays honest) or unset —
    # either way no elicitation and no crash.
    report = getattr(ctx, "_requirement_report", None)
    assert report is None or report.get("ask_bundle") == []


# ═════════════════════════════════════════════════════════════════════════════
# ask_bundle dedupe key — bound_value_ref (pruned-backlog fix, front-loaded)
# ═════════════════════════════════════════════════════════════════════════════

def _obj(n):
    from types import SimpleNamespace
    return SimpleNamespace(id=n)


def test_ask_bundle_does_not_dedupe_distinct_bound_values():
    """Two objectives binding the SAME schema_path to DIFFERENT values (distinct
    bound_value_ref) are DISTINCT asks. bound_value_ref is IN the dedupe key, so the
    second binding is NOT silently dropped from the operator's one-click bundle."""
    from systemu.runtime.requirement_binder import build_requirement_report
    r_a = _req(schema_path="repo", kind="decision", state="resolvable",
               value_origin="operator", bound_value_ref="repo:project-a")
    r_b = _req(schema_path="repo", kind="decision", state="resolvable",
               value_origin="operator", bound_value_ref="repo:project-b")

    def _cr(obj, *a, **k):
        return [r_a] if obj.id == 1 else [r_b]

    with patch("systemu.runtime.requirement_binder.compute_requirements", side_effect=_cr):
        report = build_requirement_report([_obj(1), _obj(2)], object(), {}, None)

    refs = {getattr(r, "bound_value_ref", None) for r in report.ask_bundle}
    assert refs == {"repo:project-a", "repo:project-b"}, "both distinct bindings must surface"
    assert len(report.ask_bundle) == 2


def test_ask_bundle_still_dedupes_identical_asks():
    """Regression: two objectives with an IDENTICAL ask (same key incl.
    bound_value_ref) still dedupe to one."""
    from systemu.runtime.requirement_binder import build_requirement_report
    same = _req(schema_path="repo", kind="decision", state="resolvable",
                value_origin="operator", bound_value_ref="repo:x")

    with patch("systemu.runtime.requirement_binder.compute_requirements",
               side_effect=lambda obj, *a, **k: [same]):
        report = build_requirement_report([_obj(1), _obj(2)], object(), {}, None)
    assert len(report.ask_bundle) == 1
