"""v0.9.35 (P1) — unified structured human-input (elicitation) surface.

Covers: missing_required detection (JSON-Schema shape), the elicitation
model + coercion, the _handle_tool_call detection seam, the multi-field
operator form, reconciler + grant-apply re-dispatch, URL-mode secrets,
and the ASK_OPERATOR structured upgrade.

Style mirrors tests/test_harness_grant_reconciler.py (real Vault, fake
Supervisor/Governor) and tests/test_v0933_harness_budget.py (pure-helper
unit tests + inspect.getsource wiring guards).
"""
from __future__ import annotations

import importlib
import inspect

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

class _V1Tool:
    """Minimal stand-in for a v1 Tool (only the attrs missing_required reads)."""
    def __init__(self, name, parameters_schema):
        self.name = name
        self.parameters_schema = parameters_schema


class _FakeV2Entry:
    def __init__(self, schema):
        self.schema = schema


class _FakeV2Registry:
    def __init__(self, table):
        self._table = table

    def get(self, name):
        return self._table.get(name)


class _FakeCtx:
    """Minimal ExecutionContext stand-in for _handle_tool_call seam tests."""
    def __init__(self):
        self.observations = []
        self.tool_calls = []

    def add_observation(self, obs, ab):
        self.observations.append(obs)

    def add_tool_call(self, decision, ab):
        self.tool_calls.append(decision)


def _bare_runtime():
    """Construct a ShadowRuntime without running __init__ side effects.

    We only exercise the pure detection seam (it touches no vault / network),
    so we bypass __init__ and stamp only the attributes the seam reads.
    """
    from systemu.runtime.shadow_runtime import ShadowRuntime
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.config = None
    rt.vault = None
    rt._subagent_depth = 0
    rt._same_tool_fail_streak = {}
    return rt


def _make_vault(tmp_path):
    """Filesystem Vault with the layout the resume tests use
    (verbatim from tests/test_harness_grant_reconciler.py)."""
    from systemu.vault.vault import Vault
    for sub in [
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications",
        "executions", "decisions",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in [
        "scrolls", "activities", "shadow_army", "skills", "tools",
        "evolutions", "decisions",
    ]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


_GEOCODE_SCHEMA = {
    "type": "object",
    "properties": {
        "place": {"type": "string", "description": "City or address to geocode."},
        "country": {"type": "string", "description": "ISO country code."},
    },
    "required": ["place"],
}


# ─────────────────────────────────────────────────────────────────────────────
#  Task 1 — missing_required (pure, JSON-Schema shape)
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_required_v1_flags_absent_required_field():
    from systemu.runtime.param_validation import missing_required
    tool = _V1Tool("geocode", _GEOCODE_SCHEMA)
    gap = missing_required("geocode", {"country": "US"}, tools=[tool], v2_registry=None)
    assert [f["name"] for f in gap] == ["place"]
    assert gap[0]["type"] == "string"
    assert gap[0]["description"] == "City or address to geocode."


def test_missing_required_treats_none_and_empty_string_as_absent():
    from systemu.runtime.param_validation import missing_required
    tool = _V1Tool("geocode", _GEOCODE_SCHEMA)
    for bad in (None, ""):
        gap = missing_required("geocode", {"place": bad}, tools=[tool], v2_registry=None)
        assert [f["name"] for f in gap] == ["place"]


def test_missing_required_all_present_returns_empty():
    from systemu.runtime.param_validation import missing_required
    tool = _V1Tool("geocode", _GEOCODE_SCHEMA)
    gap = missing_required("geocode", {"place": "Paris"}, tools=[tool], v2_registry=None)
    assert gap == []


def test_missing_required_empty_schema_is_zero_behavior_change():
    from systemu.runtime.param_validation import missing_required
    tool = _V1Tool("legacy", {})
    assert missing_required("legacy", {}, tools=[tool], v2_registry=None) == []
    # Unknown tool (no schema anywhere) → empty list, never raises.
    assert missing_required("ghost", {}, tools=[], v2_registry=None) == []


def test_missing_required_resolves_v2_registry_schema():
    from systemu.runtime.param_validation import missing_required
    reg = _FakeV2Registry({"geocode": _FakeV2Entry(_GEOCODE_SCHEMA)})
    gap = missing_required("geocode", {}, tools=[], v2_registry=reg)
    assert [f["name"] for f in gap] == ["place"]


def test_missing_required_carries_enum_and_format():
    from systemu.runtime.param_validation import missing_required
    schema = {
        "type": "object",
        "properties": {
            "fmt": {"type": "string", "enum": ["csv", "xlsx"], "description": "Output format."},
            "email": {"type": "string", "format": "email"},
        },
        "required": ["fmt", "email"],
    }
    tool = _V1Tool("export", schema)
    gap = missing_required("export", {}, tools=[tool], v2_registry=None)
    by_name = {f["name"]: f for f in gap}
    assert by_name["fmt"]["enum"] == ["csv", "xlsx"]
    assert by_name["email"]["format"] == "email"


# ─────────────────────────────────────────────────────────────────────────────
#  Task 2 — elicitation model + coercion (MCP form-mode shape)
# ─────────────────────────────────────────────────────────────────────────────

def test_elicitation_schema_from_fields_builds_form_mode_shape():
    from systemu.runtime.elicitation import elicitation_schema_from_fields
    fields = [
        {"name": "place", "type": "string", "description": "City."},
        {"name": "fmt", "type": "string", "enum": ["csv", "xlsx"]},
    ]
    sch = elicitation_schema_from_fields(fields)
    assert sch["type"] == "object"
    assert sch["properties"]["place"]["type"] == "string"
    assert sch["properties"]["fmt"]["enum"] == ["csv", "xlsx"]
    assert sch["required"] == ["place", "fmt"]


def test_coerce_field_value_typed():
    from systemu.runtime.elicitation import coerce_field_value
    assert coerce_field_value("number", "3.5") == 3.5
    assert coerce_field_value("integer", "7") == 7
    assert coerce_field_value("boolean", "true") is True
    assert coerce_field_value("boolean", "no") is False
    assert coerce_field_value("string", 42) == "42"
    # Non-coercible number → None (caller treats as still-missing, re-asks).
    assert coerce_field_value("number", "abc") is None


def test_validate_against_schema_reports_missing_and_bad_enum():
    from systemu.runtime.elicitation import validate_against_schema
    sch = {
        "type": "object",
        "properties": {"fmt": {"type": "string", "enum": ["csv", "xlsx"]}},
        "required": ["fmt"],
    }
    assert validate_against_schema(sch, {"fmt": "csv"}) == []
    assert validate_against_schema(sch, {}) == ["fmt"]            # missing
    assert validate_against_schema(sch, {"fmt": "pdf"}) == ["fmt"]  # not in enum


def test_is_secret_field_detects_credentials():
    from systemu.runtime.elicitation import is_secret_field
    assert is_secret_field({"name": "api_key"}) is True
    assert is_secret_field({"name": "password"}) is True
    assert is_secret_field({"name": "auth_token"}) is True
    assert is_secret_field({"name": "format", "type": "string"}) is False
    # Explicit format marker also wins.
    assert is_secret_field({"name": "x", "format": "password"}) is True


def test_param_answers_from_choice_coerces_per_field():
    from systemu.runtime.elicitation import param_answers_from_choice
    sch = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "place": {"type": "string"},
        },
        "required": ["count", "place"],
    }
    # raw choice is the JSON the form serialized (all strings from inputs)
    answers = param_answers_from_choice(sch, {"count": "12", "place": "Paris"})
    assert answers == {"count": 12, "place": "Paris"}


# ─────────────────────────────────────────────────────────────────────────────
#  Task 3 — _handle_tool_call detection seam (returns __needs_input__ sentinel)
# ─────────────────────────────────────────────────────────────────────────────

def test_handle_tool_call_has_missing_required_seam_after_coercion():
    """inspect.getsource guard: the seam calls missing_required AFTER the
    scalar coercion and BEFORE the v2 short-circuit / v1 dispatch. The
    recurring failure mode here is a green helper the loop stops calling."""
    mod = importlib.import_module("systemu.runtime.shadow_runtime")
    src = inspect.getsource(mod.ShadowRuntime._handle_tool_call)
    assert "missing_required" in src, "detection seam not wired into _handle_tool_call"
    assert "__needs_input__" in src, "seam must return the needs-input sentinel"
    # Ordering: coercion (_coerce_scalar_parameter) precedes the seam, which
    # precedes the v2 short-circuit (tool_registry_v2 import).
    i_coerce = src.index("_coerce_scalar_parameter")
    i_seam = src.index("missing_required")
    i_v2 = src.index("tool_registry_v2")
    assert i_coerce < i_seam < i_v2, "seam must sit after coercion, before v2 dispatch"


@pytest.mark.asyncio
async def test_handle_tool_call_returns_sentinel_on_missing_required(monkeypatch):
    """A non-empty gap ⇒ _handle_tool_call returns a __needs_input__ sentinel
    carrying a kind=INPUT HarnessRequest with requested_schema + pending_tool;
    the real tool is NOT dispatched."""
    rt = _bare_runtime()  # see helper above
    tool = _V1Tool("geocode", _GEOCODE_SCHEMA)
    ctx = _FakeCtx()
    decision = {"tool_name": "geocode", "parameters": {"country": "US"}}

    res = await rt._handle_tool_call(decision, [tool], ctx, current_ab=1, dry_run=False)

    assert res is not None
    assert res.parsed.get("__needs_input__") is True
    hreq = res.parsed.get("harness_request")
    assert getattr(hreq.kind, "value", hreq.kind) == "input"
    assert hreq.spec["pending_tool"]["tool_name"] == "geocode"
    assert hreq.spec["pending_tool"]["parameters"] == {"country": "US"}
    assert hreq.spec["requested_schema"]["required"] == ["place"]


@pytest.mark.asyncio
async def test_handle_tool_call_empty_schema_no_sentinel(monkeypatch):
    """Empty schema ⇒ no gap ⇒ no sentinel (regression guard: zero behavior
    change for legacy tools). The tool dispatches normally."""
    rt = _bare_runtime()
    tool = _V1Tool("legacy", {})
    tool.implementation_path = None  # forces the v1 "no implementation" path
    tool.status = "deployed"
    ctx = _FakeCtx()
    decision = {"tool_name": "legacy", "parameters": {}}

    res = await rt._handle_tool_call(decision, [tool], ctx, current_ab=1, dry_run=False)
    # No sentinel — it fell through to the normal (no-implementation) v1 path.
    assert res is None or not (getattr(res, "parsed", {}) or {}).get("__needs_input__")


# ─────────────────────────────────────────────────────────────────────────────
#  Task 4 — TOOL_CALL loop interception of the sentinel
# ─────────────────────────────────────────────────────────────────────────────

def test_tool_call_branch_intercepts_needs_input_sentinel():
    """inspect.getsource guard on ShadowRuntime.execute: the TOOL_CALL branch
    must intercept __needs_input__ and route it through the suspend rail
    (surface_harness_request + suspended_harness_escalation) — NOT count it as
    a normal tool call."""
    mod = importlib.import_module("systemu.runtime.shadow_runtime")
    src = inspect.getsource(mod.ShadowRuntime.execute)
    assert "__needs_input__" in src, "loop does not intercept the needs-input sentinel"
    # The interception must precede the tool_call_count increment.
    i_sentinel = src.index("__needs_input__")
    i_count = src.index("tool_call_count += 1")
    assert i_sentinel < i_count, "sentinel must be intercepted before counting the call"
    # It reuses the existing suspend rail, not a new status.
    assert "suspended_harness_escalation" in src
    assert "surface_harness_request" in src


def test_tool_call_branch_headless_fail_closed_observation():
    """inspect.getsource guard: the interception has a headless / no-queue
    fail-closed path emitting a missing_required_params observation (never a
    fabricated value, never a hang)."""
    mod = importlib.import_module("systemu.runtime.shadow_runtime")
    src = inspect.getsource(mod.ShadowRuntime.execute)
    assert "missing_required_params" in src
    assert "is_headless" in src


# ─────────────────────────────────────────────────────────────────────────────
#  Task 5 — GateDescriptor + surface carry requested_schema / pending_tool
# ─────────────────────────────────────────────────────────────────────────────

def _input_request():
    from systemu.core.models import HarnessRequest, HarnessKind
    sch = {
        "type": "object",
        "properties": {"place": {"type": "string", "description": "City."}},
        "required": ["place"],
    }
    return HarnessRequest(
        kind=HarnessKind.INPUT,
        spec={
            "question": "Tool 'geocode' needs more parameters.",
            "requested_schema": sch,
            "pending_tool": {"tool_name": "geocode", "parameters": {"country": "US"}},
        },
        rationale="Missing required parameter(s): place.",
    )


def _grant_verdict(req):
    from systemu.core.models import HarnessVerdict, HarnessDecision, RiskBand
    return HarnessVerdict(
        request_id=req.request_id, decision=HarnessDecision.ESCALATE,
        risk_band=RiskBand.MEDIUM, rationale="needs operator input",
    )


def test_gate_descriptor_from_harness_carries_requested_schema():
    from systemu.interface.command.gate import GateDescriptor
    req = _input_request()
    desc = GateDescriptor.from_harness(req, _grant_verdict(req), execution_id="exec_1")
    assert desc.requested_schema["required"] == ["place"]


def test_surface_harness_request_puts_schema_in_context(tmp_path):
    from systemu.interface.harness_review import surface_harness_request
    vlt = _make_vault(tmp_path)  # reuse the reconciler-test helper (Task 7 imports it)
    req = _input_request()
    did = surface_harness_request(
        req, _grant_verdict(req), execution_id="exec_1",
        activity_id="act_1", shadow_id="sh_1", vault=vlt,
    )
    dec = vlt.get_decision(did)
    assert dec.context["requested_schema"]["required"] == ["place"]
    assert dec.context["pending_tool"]["tool_name"] == "geocode"


def test_surface_harness_request_suppresses_raw_spec_dump_for_form(tmp_path):
    """Medium fix: an INPUT request with a requested_schema renders the typed
    form (Task 6) from ctx, so the card body must show only the question — NOT
    the raw `Spec: {json}` dump (which would appear as a truncated blob above
    the nice form)."""
    from systemu.interface.harness_review import surface_harness_request
    vlt = _make_vault(tmp_path)
    req = _input_request()
    did = surface_harness_request(
        req, _grant_verdict(req), execution_id="exec_1",
        activity_id="act_1", shadow_id="sh_1", vault=vlt,
    )
    dec = vlt.get_decision(did)
    body = dec.body or ""
    assert "Spec: {" not in body, "raw spec JSON dump must be suppressed for form gates"
    assert "Tool 'geocode' needs more parameters." in body  # the question is shown


def test_surface_harness_request_synthesizes_form_for_free_text_input(tmp_path):
    """v0.9.45: a free-text INPUT request (no requested_schema) now SYNTHESIZES a
    one-field schema so the card renders an answer BOX (not a raw `Spec: {json}`
    dump) — the operator types the value and the reconciler extracts it cleanly,
    ending the old re-ask futility loop."""
    from systemu.interface.harness_review import surface_harness_request
    from systemu.core.models import HarnessRequest, HarnessKind, HarnessVerdict, \
        HarnessDecision, RiskBand
    vlt = _make_vault(tmp_path)
    req = HarnessRequest(
        kind=HarnessKind.INPUT,
        spec={"question": "CSV or XLSX?"},   # no requested_schema → synthesized
        rationale="ambiguous format",
    )
    vd = HarnessVerdict(
        request_id=req.request_id, decision=HarnessDecision.ESCALATE,
        risk_band=RiskBand.MEDIUM, rationale="ask",
    )
    did = surface_harness_request(
        req, vd, execution_id="exec_2", activity_id="act_2",
        shadow_id="sh_2", vault=vlt,
    )
    dec = vlt.get_decision(did)
    # The synthesized form replaces the raw spec dump; the question is the body.
    assert "Spec: {" not in (dec.body or "")
    assert "CSV or XLSX?" in (dec.body or "")
    # The context carries the synthesized one-field schema so render draws a box.
    schema = (dec.context or {}).get("requested_schema") or {}
    assert schema.get("properties", {}).get("answer", {}).get("type") == "string"


def test_surface_harness_request_keeps_spec_dump_for_capability(tmp_path):
    """Back-compat: a NON-input capability request (no requested_schema) still
    keeps the full `Spec: {json}` preview — synthesis is scoped to kind==input."""
    from systemu.interface.harness_review import surface_harness_request
    from systemu.core.models import HarnessRequest, HarnessKind, HarnessVerdict, \
        HarnessDecision, RiskBand
    vlt = _make_vault(tmp_path)
    req = HarnessRequest(
        kind=HarnessKind.TOOL,
        spec={"name": "geocode_place"},   # capability request, no schema
        rationale="need a tool",
    )
    vd = HarnessVerdict(
        request_id=req.request_id, decision=HarnessDecision.ESCALATE,
        risk_band=RiskBand.MEDIUM, rationale="ask",
    )
    did = surface_harness_request(
        req, vd, execution_id="exec_3", activity_id="act_3",
        shadow_id="sh_3", vault=vlt,
    )
    dec = vlt.get_decision(did)
    assert "Spec: {" in (dec.body or ""), "capability gate must keep the spec preview"
    assert not ((dec.context or {}).get("requested_schema") or {})


# ─────────────────────────────────────────────────────────────────────────────
#  Task 6 — multi-field operator form (one card, all fields)
# ─────────────────────────────────────────────────────────────────────────────

def test_build_elicitation_answer_serializes_all_fields():
    from systemu.interface.pages.insights import build_elicitation_answer
    import json
    sch = {
        "type": "object",
        "properties": {
            "place": {"type": "string"},
            "fmt": {"type": "string", "enum": ["csv", "xlsx"]},
            "count": {"type": "integer"},
        },
        "required": ["place", "fmt", "count"],
    }
    raw = build_elicitation_answer(sch, {"place": "Paris", "fmt": "csv", "count": "3"})
    assert json.loads(raw) == {"place": "Paris", "fmt": "csv", "count": "3"}


def test_render_decision_card_has_elicitation_form_branch():
    """getsource guard: render_decision_card renders ONE multi-field form for an
    elicitation gate (enum→select, bool→radio, secret→URL link), keyed on
    ctx['requested_schema']; it must NOT fall through to N flat option buttons."""
    mod = importlib.import_module("systemu.interface.pages.insights")
    src = inspect.getsource(mod.render_decision_card)
    assert "requested_schema" in src, "no elicitation_form branch in render_decision_card"
    assert "build_elicitation_answer" in src
    # Secret fields go URL-mode, not a typed input.
    assert "is_secret_field" in src


# ─────────────────────────────────────────────────────────────────────────────
#  Task 7 — reconciler INPUT branch → typed param_answers grant_payload
# ─────────────────────────────────────────────────────────────────────────────

class _FakeSupervisor:
    def __init__(self):
        self.calls = []

    def resume_after_grant(self, **kw):
        self.calls.append(kw)
        return f"sub_{len(self.calls)}"


def _seed_snapshot(tmp_path, *, execution_id, shadow_id, scroll_id, activity_id):
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    data_dir = tmp_path / "data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    write_snapshot(
        ExecutionSnapshot(
            execution_id=execution_id, shadow_id=shadow_id,
            scroll_id=scroll_id, activity_id=activity_id,
            completed_objective_ids=[0],
        ),
        data_dir=data_dir,
    )
    return data_dir


def _post_resolve_input_gate(vault, *, choice, requested_schema, pending_tool,
                             execution_id="exec_i", activity_id="act_i",
                             shadow_id="sh_i", request_id="hreq_i"):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)
    did = queue.post(
        title=f"Harness request: input [{request_id}]",
        body="?",
        options=["Deny", "Approve", "Edit spec"],
        context={
            "kind": "gate", "gate_type": "harness",
            "execution_id": execution_id, "activity_id": activity_id,
            "shadow_id": shadow_id, "request_id": request_id,
            "harness_kind": "input",
            "spec": {"requested_schema": requested_schema, "pending_tool": pending_tool},
            "requested_schema": requested_schema,
            "pending_tool": pending_tool,
            "risk_band": "medium",
        },
        dedup_key=f"harness:{execution_id}:{request_id}",
    )
    queue.resolve(did, choice=choice)
    return did


def test_reconciler_input_builds_typed_param_answers(tmp_path):
    from systemu.scheduler.jobs import reconcile_resolved_harness_grants
    vlt = _make_vault(tmp_path)
    data_dir = _seed_snapshot(
        tmp_path, execution_id="exec_i", shadow_id="sh_i",
        scroll_id="sc_i", activity_id="act_i",
    )
    sch = {
        "type": "object",
        "properties": {"place": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["place", "count"],
    }
    pending = {"tool_name": "geocode", "parameters": {"country": "US"}}
    import json
    _post_resolve_input_gate(
        vlt, choice=json.dumps({"place": "Paris", "count": "3"}),
        requested_schema=sch, pending_tool=pending,
    )
    sup = _FakeSupervisor()
    n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
    assert n == 1
    gp = sup.calls[0]["grant_payload"]
    assert gp["kind"] == "input"
    assert gp["param_answers"] == {"place": "Paris", "count": 3}   # typed
    assert gp["pending_tool"]["tool_name"] == "geocode"


def test_reconciler_plain_ask_operator_unchanged(tmp_path):
    """Free-text ASK_OPERATOR (no requested_schema) keeps the operator_answer
    payload byte-identical (back-compat regression guard)."""
    from systemu.scheduler.jobs import reconcile_resolved_harness_grants
    from systemu.approval.decision_queue import OperatorDecisionQueue
    vlt = _make_vault(tmp_path)
    data_dir = _seed_snapshot(
        tmp_path, execution_id="exec_p", shadow_id="sh_p",
        scroll_id="sc_p", activity_id="act_p",
    )
    q = OperatorDecisionQueue(vlt)
    did = q.post(
        title="Harness request: input [hreq_p]", body="?",
        options=["Deny", "Approve", "Edit spec"],
        context={
            "kind": "gate", "gate_type": "harness",
            "execution_id": "exec_p", "activity_id": "act_p", "shadow_id": "sh_p",
            "request_id": "hreq_p", "harness_kind": "input",
            "spec": {"question": "CSV or XLSX?"}, "risk_band": "medium",
        },
        dedup_key="harness:exec_p:hreq_p",
    )
    q.resolve(did, choice="CSV")
    sup = _FakeSupervisor()
    n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
    assert n == 1
    gp = sup.calls[0]["grant_payload"]
    assert gp["kind"] == "input"
    assert gp["operator_answer"] == "CSV"
    assert "param_answers" not in gp


# ─────────────────────────────────────────────────────────────────────────────
#  Task 8 — _apply_harness_grant INPUT branch: merge + re-dispatch
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_harness_grant_input_merges_and_redispatches():
    """param_answers + pending_tool ⇒ merge into parameters and re-dispatch via
    the injected redispatch closure (re-validates). The closure receives the
    completed decision."""
    rt = _bare_runtime()
    ctx = _FakeCtx()
    seen = {}

    async def _redispatch(decision):
        seen["decision"] = decision
        from systemu.runtime.shadow_runtime import ToolResult
        return ToolResult(success=True, parsed={"ok": True})

    rt._resume_redispatch = _redispatch
    payload = {
        "kind": "input",
        "param_answers": {"place": "Paris"},
        "pending_tool": {"tool_name": "geocode", "parameters": {"country": "US"}},
    }
    budget = await rt._apply_harness_grant_async(
        payload, context=ctx, tools=[], tool_index=[], current_ab=1, iter_budget=30,
    )
    assert budget == 30
    assert seen["decision"]["tool_name"] == "geocode"
    # Merged: original country + the operator-supplied place.
    assert seen["decision"]["parameters"] == {"country": "US", "place": "Paris"}


@pytest.mark.asyncio
async def test_apply_harness_grant_input_empty_answers_fail_closed():
    """Decline / non-coercible ⇒ empty param_answers ⇒ harness_grant_failed
    observation; the tool is NOT re-dispatched (never fabricate)."""
    rt = _bare_runtime()
    ctx = _FakeCtx()
    called = {"n": 0}

    async def _redispatch(decision):
        called["n"] += 1
        return None

    rt._resume_redispatch = _redispatch
    payload = {
        "kind": "input",
        "param_answers": {},
        "pending_tool": {"tool_name": "geocode", "parameters": {"country": "US"}},
    }
    await rt._apply_harness_grant_async(
        payload, context=ctx, tools=[], tool_index=[], current_ab=1, iter_budget=30,
    )
    assert called["n"] == 0
    assert any(o.get("type") == "harness_grant_failed" for o in ctx.observations)


@pytest.mark.asyncio
async def test_apply_harness_grant_plain_input_unchanged():
    """A plain operator_answer (no param_answers) keeps the observation-only
    behavior (back-compat)."""
    rt = _bare_runtime()
    ctx = _FakeCtx()
    payload = {"kind": "input", "operator_answer": "CSV"}
    await rt._apply_harness_grant_async(
        payload, context=ctx, tools=[], tool_index=[], current_ab=1, iter_budget=30,
    )
    assert any(o.get("type") == "harness_granted"
               and "CSV" in o.get("message", "") for o in ctx.observations)


# ─────────────────────────────────────────────────────────────────────────────
#  Task 9 — execute() wires the resume re-dispatch closure
# ─────────────────────────────────────────────────────────────────────────────

def test_execute_uses_apply_harness_grant_async_with_redispatch():
    """getsource guard: the resume-start apply uses the async helper and sets a
    _resume_redispatch closure bound to _handle_tool_call."""
    mod = importlib.import_module("systemu.runtime.shadow_runtime")
    src = inspect.getsource(mod.ShadowRuntime.execute)
    assert "_apply_harness_grant_async" in src, "resume-start must use the async apply"
    assert "_resume_redispatch" in src, "resume-start must bind a re-dispatch closure"
    # The closure must call _handle_tool_call (the re-validation chokepoint).
    i_closure = src.index("_resume_redispatch")
    assert "_handle_tool_call" in src[i_closure:i_closure + 600], (
        "the re-dispatch closure must call _handle_tool_call to re-validate"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Task 10 — URL-mode secrets never reach the form / param_answers / logs
# ─────────────────────────────────────────────────────────────────────────────

def test_split_secret_fields_partitions():
    from systemu.runtime.elicitation import split_secret_fields
    form, secret = split_secret_fields([
        {"name": "place", "type": "string"},
        {"name": "api_key", "type": "string"},
        {"name": "fmt", "type": "string", "format": "password"},
    ])
    assert [f["name"] for f in form] == ["place"]
    assert {f["name"] for f in secret} == {"api_key", "fmt"}


def test_param_answers_never_includes_secret_not_in_schema():
    """param_answers_from_choice only emits schema 'properties' fields — a secret
    that was correctly excluded from requested_schema can never be coerced in
    even if a value is injected into the raw dict."""
    from systemu.runtime.elicitation import param_answers_from_choice
    # requested_schema EXCLUDES the secret (split out at build time).
    sch = {"type": "object", "properties": {"place": {"type": "string"}},
           "required": ["place"]}
    answers = param_answers_from_choice(sch, {"place": "Paris", "api_key": "sk-LEAK"})
    assert answers == {"place": "Paris"}
    assert "api_key" not in answers


def test_elicitation_schema_excludes_secrets_when_split_first():
    """The seam builds the form schema from the NON-secret fields only — a
    secret field never enters requested_schema (so it can't be rendered/logged)."""
    from systemu.runtime.elicitation import (
        split_secret_fields, elicitation_schema_from_fields,
    )
    fields = [
        {"name": "place", "type": "string"},
        {"name": "password", "type": "string"},
    ]
    form, _secret = split_secret_fields(fields)
    sch = elicitation_schema_from_fields(form)
    assert "password" not in sch["properties"]
    assert sch["required"] == ["place"]


# ─────────────────────────────────────────────────────────────────────────────
#  Task 11 — ASK_OPERATOR structured fields upgrade (back-compatible)
# ─────────────────────────────────────────────────────────────────────────────

def test_ask_operator_threads_requested_schema():
    """getsource guard: the ASK_OPERATOR branch threads a requested_schema from
    the decision into the INPUT request spec when supplied (no fields ⇒
    unchanged free-text)."""
    mod = importlib.import_module("systemu.runtime.shadow_runtime")
    src = inspect.getsource(mod.ShadowRuntime.execute)
    # Find the ASK_OPERATOR HarnessRequest build region.
    i = src.index('if action == "ASK_OPERATOR":')
    region = src[i:i + 700]
    assert "requested_schema" in region, (
        "ASK_OPERATOR must thread a structured requested_schema when supplied"
    )


def test_execute_step_md_documents_ask_operator_fields():
    import pathlib
    md = pathlib.Path("systemu/prompts/execute_step.md").read_text(encoding="utf-8")
    assert "requested_schema" in md, "execute_step.md must document ASK_OPERATOR fields"


# ─────────────────────────────────────────────────────────────────────────────
#  Task 12 — request_choice optional requested_schema pass-through
# ─────────────────────────────────────────────────────────────────────────────

def test_request_choice_accepts_requested_schema_kwarg():
    """request_choice signature accepts requested_schema and stamps it into the
    posted context (so the elicitation form renders). Back-compat: absent ⇒
    unchanged."""
    import inspect as _ins
    from systemu.interface import notifications
    sig = _ins.signature(notifications.request_choice)
    assert "requested_schema" in sig.parameters, (
        "request_choice must accept an optional requested_schema kwarg"
    )
    src = _ins.getsource(notifications.request_choice)
    assert '"requested_schema"' in src or "'requested_schema'" in src, (
        "request_choice must stamp requested_schema into the posted context"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Task 13 — resolve_structured_input (exported resolver; P2 Task-13 entry point)
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_structured_input_is_exported_with_pinned_signature():
    """Pinned-contract guard: elicitation.py EXPORTS resolve_structured_input
    with EXACTLY the frozen kw-only signature (message, requested_schema, vault=,
    config=). P2 Task 13 imports this name."""
    import inspect as _ins
    from systemu.runtime import elicitation
    assert hasattr(elicitation, "resolve_structured_input"), (
        "elicitation.py must EXPORT resolve_structured_input (pinned contract B6)"
    )
    sig = _ins.signature(elicitation.resolve_structured_input)
    params = sig.parameters
    assert params["message"].kind is _ins.Parameter.KEYWORD_ONLY
    assert params["requested_schema"].kind is _ins.Parameter.KEYWORD_ONLY
    assert params["vault"].default is None
    assert params["config"].default is None


_RSI_SCHEMA = {
    "type": "object",
    "properties": {"place": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["place", "count"],
}


def test_resolve_structured_input_no_queue_returns_cancel(monkeypatch):
    """No operator queue / non-interactive ⇒ cancel (fail-closed: never hang,
    never fabricate)."""
    from systemu.runtime import elicitation
    monkeypatch.setattr(
        "systemu.interface.notifications.request_choice",
        lambda *a, **k: None,
    )
    out = elicitation.resolve_structured_input(
        message="Need params", requested_schema=_RSI_SCHEMA,
    )
    assert out == {"action": "cancel", "content": {}}


def test_resolve_structured_input_accept_returns_typed_content(monkeypatch):
    """A resolved form answer ⇒ accept with TYPE-COERCED content (same coercion
    as the reconciler's param_answers_from_choice)."""
    from systemu.runtime import elicitation
    captured = {}

    def _fake_request_choice(questions, *, dedup_key, extra_context=None,
                             requested_schema=None):
        captured["dedup_key"] = dedup_key
        captured["requested_schema"] = requested_schema
        captured["prompt"] = questions[0]["prompt"]
        # The poster returns the form's per-field answer dict (strings).
        return {"place": "Paris", "count": "3"}

    monkeypatch.setattr(
        "systemu.interface.notifications.request_choice", _fake_request_choice,
    )
    out = elicitation.resolve_structured_input(
        message="Need params", requested_schema=_RSI_SCHEMA,
    )
    assert out["action"] == "accept"
    assert out["content"] == {"place": "Paris", "count": 3}   # count coerced to int
    # The resolver drove the SAME form rail: passed requested_schema + message.
    assert captured["requested_schema"] == _RSI_SCHEMA
    assert captured["prompt"] == "Need params"
    assert captured["dedup_key"].startswith("elicit:")


def test_resolve_structured_input_decline_marker_returns_decline(monkeypatch):
    """The safe-default / Deny choice (poster returns a {'_raw': 'Deny'} marker)
    ⇒ decline, never accept (never fabricate)."""
    from systemu.runtime import elicitation
    monkeypatch.setattr(
        "systemu.interface.notifications.request_choice",
        lambda *a, **k: {"_raw": "Deny"},
    )
    out = elicitation.resolve_structured_input(
        message="Need params", requested_schema=_RSI_SCHEMA,
    )
    assert out == {"action": "decline", "content": {}}


def test_resolve_structured_input_empty_or_noncoercible_returns_cancel(monkeypatch):
    """A resolved answer that coerces to nothing (still-missing) ⇒ cancel
    (never an accept with a fabricated value)."""
    from systemu.runtime import elicitation
    monkeypatch.setattr(
        "systemu.interface.notifications.request_choice",
        lambda *a, **k: {"count": "not-a-number"},   # non-coercible int → dropped
    )
    out = elicitation.resolve_structured_input(
        message="Need params", requested_schema=_RSI_SCHEMA,
    )
    assert out == {"action": "cancel", "content": {}}


def test_resolve_structured_input_propagates_pending(monkeypatch):
    """While awaiting the operator, request_choice raises PendingChoiceRequest —
    resolve_structured_input must let it PROPAGATE (the suspend is the rail)."""
    from systemu.runtime import elicitation
    from systemu.approval.exceptions import PendingChoiceRequest

    def _raise_pending(*a, **k):
        raise PendingChoiceRequest(decision_id="dec_x", dedup_key="elicit:x",
                                   options=["Submit"])

    monkeypatch.setattr(
        "systemu.interface.notifications.request_choice", _raise_pending,
    )
    with pytest.raises(PendingChoiceRequest):
        elicitation.resolve_structured_input(
            message="Need params", requested_schema=_RSI_SCHEMA,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Task 14 — golden: INPUT elicitation suspend → resolve → resume (reconciler)
# ─────────────────────────────────────────────────────────────────────────────

def test_golden_input_elicitation_round_trip(tmp_path):
    from systemu.scheduler.jobs import reconcile_resolved_harness_grants
    import json
    vlt = _make_vault(tmp_path)
    data_dir = _seed_snapshot(
        tmp_path, execution_id="exec_g", shadow_id="sh_g",
        scroll_id="sc_g", activity_id="act_g",
    )
    sch = {
        "type": "object",
        "properties": {"place": {"type": "string"}},
        "required": ["place"],
    }
    pending = {"tool_name": "geocode", "parameters": {"country": "US"}}
    did = _post_resolve_input_gate(
        vlt, choice=json.dumps({"place": "Paris"}),
        requested_schema=sch, pending_tool=pending,
        execution_id="exec_g", activity_id="act_g", shadow_id="sh_g",
        request_id="hreq_g",
    )
    sup = _FakeSupervisor()
    n1 = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
    assert n1 == 1
    kw = sup.calls[0]
    assert kw["execution_id"] == "exec_g"
    assert kw["activity_id"] == "act_g"
    assert kw["shadow_id"] == "sh_g"
    gp = kw["grant_payload"]
    assert gp["param_answers"] == {"place": "Paris"}
    assert gp["pending_tool"] == pending

    # Idempotent: the persisted flag is stamped; a second pass is a no-op.
    after = vlt.get_decision(did)
    assert after.context.get("harness_grant_dispatched") is True
    n2 = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
    assert n2 == 0
    assert len(sup.calls) == 1
