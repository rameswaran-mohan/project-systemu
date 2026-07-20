"""R-B5 / T5 — the OnTheTable planner payoff (§5.10.c chips, §5.10.d, §10, AC4b).

Four surfaces, and one structural fact that governs all of them:

  * table ATTRIBUTION on the bind (``Requirement.table_item_id``) — the enabler;
  * the §10 inventory-hit metric;
  * the §5.10.c during-task + completion chips (≤1, novelty-gated);
  * the §5.10.b#6 / AC4b capture exclusion.

THE STRUCTURAL FACT (``test_a_table_backed_bind_is_never_silent``): the IMPL-5
clamp in ``_entry_origin`` makes every table-backed bind ``content_derived``, and
``_needs_ask`` therefore always asks. So "answered from your table" can never mean
"bound with no operator interaction" — a chip written that way would read zero
forever. These tests pin the clamp AND pin what the surfaces count instead.

The integration tests deliberately build REAL ``SituationReport``/``CapabilityRef``/
``TableItem`` objects and run the REAL ``compose_table`` before the REAL binder,
rather than hand-rolling a dict that already has the annotation. Hand-rolling would
pass while ``compose_table`` did nothing at all.
"""
from __future__ import annotations

import os

import pytest

from systemu.core.models import Objective, Requirement, Tool
from systemu.runtime import table_payoff
from systemu.runtime.requirement_binder import compute_requirements, _needs_ask
from systemu.runtime.situational_inventory import (
    CapabilityRef,
    ConnectedService,
    SituationReport,
    compose_table,
)
from systemu.runtime.table_store import TableItem


# ── harness ─────────────────────────────────────────────────────────────────
class _Ctx:
    def __init__(self, situation=None):
        self._situation_report = situation
        self._granted_roots = None
        self.files_produced = []
        self.vault = None


def _tool(schema):
    return Tool(id="tool_t", name="t", description="d", tool_type="python_function",
                parameters_schema=schema)


def _obj(goal="do the thing"):
    return Objective(id=1, goal=goal, success_criteria="done")


def _req(**over):
    base = dict(kind="decision", schema_path="/p", state="have", source="situation",
                value_origin="content_derived", confidence=0.8)
    base.update(over)
    return Requirement(**base)


class _Report:
    """A RequirementReport-shaped object (per_objective + ask_bundle)."""

    def __init__(self, reqs, ask=None):
        self.per_objective = {1: list(reqs)}
        self.ask_bundle = list(ask or [])


# ═══ 1. attribution, through the REAL compose_table → binder path ═══════════
def _composed_situation(item, *, tool_id="github_push"):
    """A REAL SituationReport carrying a REAL live capability, annotated by the
    REAL compose_table from a REAL TableItem. Returned as the model_dump the
    binder sees on the live path."""
    report = SituationReport(capabilities=[CapabilityRef(tool_id=tool_id)])
    composed = compose_table(report, [item])
    return composed


def test_compose_table_actually_annotates_the_live_entry():
    """Guard for every test below: if compose_table stopped annotating, the
    attribution tests would be asserting against their own hand-built fixture."""
    item = TableItem(id="t-1", kind="tool", name="GitHub push",
                     ref={"tool_id": "github_push"})
    composed = _composed_situation(item)
    assert composed.capabilities[0].curated is True
    assert composed.capabilities[0].table_item_id == "t-1"


def test_a_curated_capability_bind_carries_the_table_item_id():
    """The payoff enabler: a bind won by a table-annotated entry names the item."""
    item = TableItem(id="t-1", kind="tool", name="github_push",
                     ref={"tool_id": "github_push"})
    composed = _composed_situation(item)
    situation = composed.model_dump(mode="json")

    schema = {"type": "object", "required": ["github_push"],
              "properties": {"github_push": {"type": "string"}}}
    reqs = compute_requirements(_obj(), _tool(schema), situation, _Ctx())
    bound = [r for r in reqs if r.source == "situation"]
    assert bound, "the curated capability should have bound the leaf"
    assert bound[0].table_item_id == "t-1"


def test_a_declared_intent_bind_carries_the_table_item_id():
    """A table item matching NO live entry falls through to declared_intents — the
    add-only channel — and a bind from it is still attributed."""
    item = TableItem(id="t-2", kind="tool", name="salesforce_sync",
                     ref={"tool_id": "salesforce_sync"})
    composed = compose_table(SituationReport(), [item])
    assert composed.declared_intents, "unmatched item must reach declared_intents"
    situation = composed.model_dump(mode="json")

    schema = {"type": "object", "required": ["salesforce_sync"],
              "properties": {"salesforce_sync": {"type": "string"}}}
    reqs = compute_requirements(_obj(), _tool(schema), situation, _Ctx())
    bound = [r for r in reqs if r.source == "situation"]
    assert bound and bound[0].table_item_id == "t-2"


def test_a_non_table_inventory_bind_is_not_attributed():
    """The discriminator. A live capability with NO table item behind it must bind
    with table_item_id=None — otherwise every inventory bind would light the chip
    and the metric would measure nothing."""
    situation = SituationReport(
        capabilities=[CapabilityRef(tool_id="github_push")]
    ).model_dump(mode="json")

    schema = {"type": "object", "required": ["github_push"],
              "properties": {"github_push": {"type": "string"}}}
    reqs = compute_requirements(_obj(), _tool(schema), situation, _Ctx())
    bound = [r for r in reqs if r.source == "situation"]
    assert bound and bound[0].table_item_id is None


def test_a_curated_service_bind_carries_the_table_item_id():
    """The service branch is a separate return site from the capability branch —
    a fix applied to one does not reach the other."""
    item = TableItem(id="t-3", kind="service", name="GitHub",
                     ref={"server": "github"})
    report = SituationReport(services=[
        ConnectedService(name="github", auth_kind="oauth", has_live_token=True,
                         account="octocat"),
    ])
    composed = compose_table(report, [item])
    assert composed.services[0].table_item_id == "t-3"
    situation = composed.model_dump(mode="json")

    schema = {"type": "object", "required": ["account"],
              "properties": {"account": {"type": "string"}}}
    reqs = compute_requirements(_obj(), _tool(schema), situation, _Ctx())
    bound = [r for r in reqs if r.source == "situation"]
    assert bound and bound[0].table_item_id == "t-3"


def test_a_file_from_a_curated_root_is_attributed_to_that_root(tmp_path):
    """A `data_root` is the item kind operators most often curate. The file bind
    goes through source #1 (``_bind_filehandle``), NOT the inventory-entry source,
    so it is a separate return site — attribution added to one does not reach it.
    """
    root = tmp_path / "granted"
    root.mkdir()
    doc = root / "report.docx"
    doc.write_bytes(b"x")

    class _Granted:
        def is_within_granted(self, c):
            c = os.path.normcase(os.path.abspath(str(c)))
            r = os.path.normcase(os.path.abspath(str(root)))
            return c == r or c.startswith(r + os.sep)

    item = TableItem(id="t-root", kind="data_root", name="Reports",
                     ref={"root_path": str(root)})
    report = SituationReport(roots=[{
        "path": str(root),
        "salient": [{"path": str(doc), "name": "report.docx", "ext": ".docx",
                     "size": 1, "mtime": os.path.getmtime(doc)}],
    }])
    composed = compose_table(report, [item])
    assert composed.roots[0].table_item_id == "t-root", "compose_table did not annotate"

    ctx = _Ctx(composed.model_dump(mode="json"))
    ctx._granted_roots = _Granted()
    schema = {"type": "object", "required": ["files"],
              "properties": {"files": {"type": "string"}}}
    reqs = compute_requirements(_obj("open the report document"), _tool(schema),
                                composed.model_dump(mode="json"), ctx)
    bound = [r for r in reqs if str(r.bound_value_ref or "").startswith("file:")]
    assert bound, "the salient file should have bound the path leaf"
    assert bound[0].table_item_id == "t-root"


def test_a_file_from_an_uncurated_root_is_not_attributed(tmp_path):
    """The discriminator for the root branch."""
    root = tmp_path / "plain"
    root.mkdir()
    doc = root / "report.docx"
    doc.write_bytes(b"x")

    class _Granted:
        def is_within_granted(self, c):
            c = os.path.normcase(os.path.abspath(str(c)))
            r = os.path.normcase(os.path.abspath(str(root)))
            return c == r or c.startswith(r + os.sep)

    situation = SituationReport(roots=[{
        "path": str(root),
        "salient": [{"path": str(doc), "name": "report.docx", "ext": ".docx",
                     "size": 1, "mtime": os.path.getmtime(doc)}],
    }]).model_dump(mode="json")

    ctx = _Ctx(situation)
    ctx._granted_roots = _Granted()
    schema = {"type": "object", "required": ["files"],
              "properties": {"files": {"type": "string"}}}
    reqs = compute_requirements(_obj("open the report document"), _tool(schema),
                                situation, ctx)
    bound = [r for r in reqs if str(r.bound_value_ref or "").startswith("file:")]
    assert bound and bound[0].table_item_id is None


@pytest.mark.parametrize("child_first", [False, True])
def test_root_attribution_prefers_the_most_specific_root(child_first):
    """A curated sub-root nested in a curated parent wins on LONGEST PREFIX, so the
    chip names the folder the operator actually curated for this material.

    Parameterised over BOTH list orderings on purpose. An earlier single-ordering
    version of this test survived a mutation that replaced longest-prefix with
    last-match-wins: with the child listed second the two rules agree, so the pin
    passed for the wrong reason. Order must not decide the answer.
    """
    from systemu.runtime.requirement_binder import _root_table_id

    parent = os.path.abspath(os.path.join("C:" + os.sep, "data"))
    child = os.path.join(parent, "invoices")
    rows = [{"path": parent, "table_item_id": "t-parent"},
            {"path": child, "table_item_id": "t-child"}]
    if child_first:
        rows.reverse()

    class _BC:
        situation = {"roots": rows}

    assert _root_table_id(_BC(), os.path.join(child, "jan.pdf")) == "t-child"
    assert _root_table_id(_BC(), os.path.join(parent, "notes.txt")) == "t-parent"


def test_root_attribution_is_component_boundary_safe():
    """`/data` must not claim a file under `/database`."""
    from systemu.runtime.requirement_binder import _root_table_id

    class _BC:
        situation = {"roots": [{"path": os.path.abspath(os.path.join("C:" + os.sep, "data")),
                                "table_item_id": "t-data"}]}

    other = os.path.abspath(os.path.join("C:" + os.sep, "database", "x.txt"))
    assert _root_table_id(_BC(), other) is None


def test_attribution_never_changes_the_taint_or_the_ask():
    """Attribution is decorative. A table-backed bind must be byte-identical in
    taint/state/confidence to the same bind without a table item behind it."""
    schema = {"type": "object", "required": ["github_push"],
              "properties": {"github_push": {"type": "string"}}}

    plain = SituationReport(capabilities=[CapabilityRef(tool_id="github_push")])
    curated = compose_table(
        SituationReport(capabilities=[CapabilityRef(tool_id="github_push")]),
        [TableItem(id="t-1", kind="tool", name="p", ref={"tool_id": "github_push"})],
    )

    a = [r for r in compute_requirements(_obj(), _tool(schema),
                                         plain.model_dump(mode="json"), _Ctx())
         if r.source == "situation"][0]
    b = [r for r in compute_requirements(_obj(), _tool(schema),
                                         curated.model_dump(mode="json"), _Ctx())
         if r.source == "situation"][0]

    assert (a.value_origin, a.state, a.confidence) == (b.value_origin, b.state, b.confidence)
    assert a.table_item_id is None and b.table_item_id == "t-1"


def test_a_forged_table_item_id_on_a_survey_entry_cannot_launder_taint():
    """A poisoned/rehydrated report claiming operator origin AND a table id still
    clamps to content_derived — attribution must never become a trust channel."""
    situation = {
        "services": [], "roots": [], "credentials": [], "profile": {},
        "capabilities": [{"tool_id": "github_push", "curated": True,
                          "table_item_id": "t-evil", "origin_class": "operator"}],
        "declared_intents": [],
    }
    schema = {"type": "object", "required": ["github_push"],
              "properties": {"github_push": {"type": "string"}}}
    reqs = compute_requirements(_obj(), _tool(schema), situation, _Ctx())
    bound = [r for r in reqs if r.source == "situation"][0]
    assert bound.value_origin == "content_derived", "forged operator origin laundered"
    assert bound.table_item_id == "t-evil"


# ═══ 2. THE structural fact ════════════════════════════════════════════════
def test_a_table_backed_bind_is_never_silent():
    """The load-bearing pin behind table_payoff's whole design.

    ``_entry_origin`` clamps every surveyed entry to content_derived, so a
    table-backed bind ALWAYS lands in the ask_bundle. Any chip or metric defined as
    "table-supplied AND not asked" is therefore structurally always zero — the
    IndexRow.effect_tags failure shape.

    If this test ever fails because someone relaxed the clamp, that is a SECURITY
    change, not a chip fix: read table_payoff's module docstring before touching it.
    """
    item = TableItem(id="t-1", kind="tool", name="p", ref={"tool_id": "github_push"})
    situation = _composed_situation(item).model_dump(mode="json")
    schema = {"type": "object", "required": ["github_push"],
              "properties": {"github_push": {"type": "string"}}}
    bound = [r for r in compute_requirements(_obj(), _tool(schema), situation, _Ctx())
             if r.source == "situation"][0]

    assert bound.value_origin == "content_derived"
    assert _needs_ask(bound) is True, (
        "a table-backed bind went silent — the IMPL-5 clamp has been relaxed; "
        "table_payoff's counting model assumes it holds"
    )


def test_the_ask_predicate_is_the_binders_own_object_not_a_copy():
    """DEC-25 hardening — the IDENTITY half.

    ``table_payoff`` used to hand-mirror ``_needs_ask`` as ``_is_ask``, and the old
    pin here asserted only that the two AGREED. That is the weak form: two paths that
    agree today can be edited together and the pin passes either way — which is
    exactly how a mutation survives. The mirror is gone; the module imports the
    binder's predicate. Assert OBJECT IDENTITY, not equality of results, so no
    reimplementation can hide behind the name.
    """
    assert table_payoff._needs_ask is _needs_ask, (
        "table_payoff re-introduced its own ask predicate — the DEC-25 tripwire rests "
        "on this predicate and a mirror lets it read a healthy zero while the real "
        "binder has changed underneath it"
    )
    assert not hasattr(table_payoff, "_is_ask"), "the hand-mirror came back"


def test_the_metric_actually_routes_through_that_predicate(monkeypatch):
    """DEC-25 hardening — the CALL-SITE half.

    Identity alone does not prove USE: the name could sit imported-but-unused while
    the counter is computed by an inline copy. Invert the predicate and require every
    derived counter to move. This is the mutation the identity pin cannot see.
    """
    rep = _Report([
        _req(schema_path="/a", state="have", value_origin="operator"),   # silent
        _req(schema_path="/b", state="have", value_origin="content_derived"),  # asks
    ])
    honest = table_payoff.inventory_hit_report(rep)
    assert (honest["silent"], honest["prefilled_confirm"]) == (1, 1)

    monkeypatch.setattr(table_payoff, "_needs_ask", lambda r: True)
    none_silent = table_payoff.inventory_hit_report(rep)
    assert none_silent["silent"] == 0, "inventory_hit_report bypassed _needs_ask"
    assert none_silent["prefilled_confirm"] == 2

    monkeypatch.setattr(table_payoff, "_needs_ask", lambda r: False)
    all_silent = table_payoff.inventory_hit_report(rep)
    assert all_silent["silent"] == 2, "inventory_hit_report bypassed _needs_ask"


def test_the_tripwire_counter_routes_through_that_predicate(monkeypatch):
    """The same call-site proof for ``table_silent`` specifically — it is a SEPARATE
    comprehension from ``silent`` and a bypass fixed in one does not reach the other.
    """
    rep = _Report([_req(schema_path="/a", state="have",
                        value_origin="content_derived", table_item_id="t-1")])
    assert table_payoff.inventory_hit_report(rep)["table_silent"] == 0

    monkeypatch.setattr(table_payoff, "_needs_ask", lambda r: False)
    assert table_payoff.inventory_hit_report(rep)["table_silent"] == 1, (
        "the table_silent comprehension does not go through _needs_ask"
    )


# ═══ 2b. DEC-25 — the clamp-regression tripwire ════════════════════════════
def test_tripwire_is_quiet_on_a_healthy_run():
    """Healthy IS zero. The alarm must not cry wolf on the normal path, or it will
    be tuned out and stop being an alarm."""
    item = TableItem(id="t-1", kind="tool", name="p", ref={"tool_id": "github_push"})
    situation = _composed_situation(item).model_dump(mode="json")
    schema = {"type": "object", "required": ["github_push"],
              "properties": {"github_push": {"type": "string"}}}
    reqs = compute_requirements(_obj(), _tool(schema), situation, _Ctx())

    out = table_payoff.clamp_tripwire(_Report(reqs))
    assert out["fired"] is False
    assert out["evaluated"] is True, "a healthy run must be EVALUATED, not skipped"
    assert out["table_silent"] == 0


def test_tripwire_FIRES_when_the_real_clamp_is_punched(monkeypatch):
    """THE proof. Not a hand-built fixture — punch the ACTUAL security control
    (``_entry_origin``, the IMPL-5 clamp) and drive the REAL binder through it.

    A hand-rolled Requirement with value_origin="operator" would prove only that a
    comprehension counts; it would pass even if the clamp and the tripwire had
    nothing to do with each other. Mutating the clamp itself is what shows the wire
    is attached to the thing it claims to watch.
    """
    import systemu.runtime.requirement_binder as rb

    item = TableItem(id="t-1", kind="tool", name="p", ref={"tool_id": "github_push"})
    situation = _composed_situation(item).model_dump(mode="json")
    schema = {"type": "object", "required": ["github_push"],
              "properties": {"github_push": {"type": "string"}}}

    # sanity: the clamp holds before we punch it
    clean = compute_requirements(_obj(), _tool(schema), situation, _Ctx())
    assert table_payoff.clamp_tripwire(_Report(clean))["fired"] is False

    # punch it — a forged "operator" origin on a surveyed inventory entry
    monkeypatch.setattr(rb, "_entry_origin", lambda entry: rb._OPERATOR)
    reqs = compute_requirements(_obj(), _tool(schema), situation, _Ctx())
    bound = [r for r in reqs if r.source == "situation"]
    assert bound and bound[0].value_origin == "operator", "the punch did not land"
    assert _needs_ask(bound[0]) is False, "the punched bind is not actually silent"

    out = table_payoff.clamp_tripwire(_Report(reqs))
    assert out["fired"] is True, "the clamp was punched and the tripwire stayed quiet"
    assert out["evaluated"] is True
    assert out["table_silent"] == 1
    assert "CLAMP REGRESSION" in (out["message"] or "")


def test_tripwire_surfaces_in_the_formatted_metric():
    """A counter nobody renders is not a tripwire. The §10 surface must SAY it."""
    rep = table_payoff.inventory_hit_report(_Report([
        _req(schema_path="/a", state="have", value_origin="operator",
             table_item_id="t-1"),
    ]))
    assert rep["table_silent"] == 1
    lines = table_payoff.format_inventory_hit(rep)
    assert any("CLAMP REGRESSION" in ln for ln in lines), lines


def test_the_healthy_surface_does_not_mention_the_alarm():
    """Discriminator for the test above — otherwise a surface that ALWAYS printed
    the banner would pass it."""
    rep = table_payoff.inventory_hit_report(_Report([
        _req(schema_path="/a", state="have", value_origin="content_derived",
             table_item_id="t-1"),
    ]))
    assert rep["table_silent"] == 0
    assert not any("CLAMP REGRESSION" in ln
                   for ln in table_payoff.format_inventory_hit(rep))


def test_junk_input_is_an_honest_zero_not_an_error():
    """Scoping note for the test below. A junk report is HANDLED (``_requirements``
    degrades to []), so there genuinely were no table-sourced silent binds and zero is
    the truthful answer — reported alongside ``supplied == 0``, which is what tells a
    reader the run was empty rather than clean."""
    for junk in (None, 12345, "nope", object()):
        rep = table_payoff.inventory_hit_report(junk)
        assert rep["supplied"] == 0 and rep["table_silent"] == 0
        assert table_payoff.clamp_tripwire(junk)["fired"] is False


def test_tripwire_reports_unknown_rather_than_healthy_when_it_cannot_look(monkeypatch):
    """FAIL-CLOSED reporting on the REAL error path.

    Zero is this wire's HEALTHY reading, so ``inventory_hit_report``'s ``except``
    branch must NOT return 0 for it — that would be an alarm reporting "clamp intact"
    precisely when it failed to evaluate. Every payoff counter degrades to 0; this one
    degrades to None.
    """
    monkeypatch.setattr(table_payoff, "_needs_ask",
                        lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    rep = table_payoff.inventory_hit_report(_Report([_req(schema_path="/a")]))
    assert rep["table_silent"] is None, (
        "the metric's error path returned a HEALTHY zero for the tripwire"
    )
    assert rep["silent"] == 0, "payoff counters should still degrade to 0"

    out = table_payoff.clamp_tripwire(_Report([_req(schema_path="/a")]))
    assert out["evaluated"] is False and out["fired"] is False
    assert "NOT EVALUATED" in (out["message"] or "")

    lines = table_payoff.format_inventory_hit({"table_silent": None, "supplied": 0})
    assert any("NOT EVALUATED" in ln for ln in lines), lines


def test_tripwire_reads_the_snapshot_dict_shape():
    """A resumed report is dicts. A model-only tripwire would read zero forever in
    production while every unit test passed — the failure shape this repo keeps
    hitting."""
    rep = _Report([_req(schema_path="/a", state="have", value_origin="operator",
                        table_item_id="t-1")])
    as_dicts = type("R", (), {
        "per_objective": {1: [r.model_dump(mode="json") for r in rep.per_objective[1]]},
        "ask_bundle": [],
    })()
    assert table_payoff.clamp_tripwire(as_dicts)["fired"] is True


def test_an_untabled_silent_bind_does_not_trip_the_wire():
    """Scope discriminator. ``silent`` via the credential-NAME branch is legitimate
    and reachable; only TABLE-attributed silence is structurally impossible. A wire
    that fired on both would be noisy and get muted."""
    rep = table_payoff.inventory_hit_report(_Report([
        _req(schema_path="/a", state="have", value_origin="operator"),  # no table id
    ]))
    assert rep["silent"] == 1 and rep["table_silent"] == 0
    assert table_payoff.clamp_tripwire(_Report([
        _req(schema_path="/a", state="have", value_origin="operator"),
    ]))["fired"] is False


# ═══ 3. the §10 inventory-hit metric ═══════════════════════════════════════
def test_metric_counts_gap_avoidance_not_silence():
    """The honest numerator: a content_derived table bind still counts as a hit
    because it turned a from-scratch gap into a pre-filled confirm."""
    rep = _Report([
        _req(schema_path="/a", state="have", value_origin="content_derived",
             table_item_id="t-1"),
        _req(schema_path="/b", state="missing", source="schema", value_origin=None),
        _req(schema_path="/c", state="have", value_origin="operator"),
    ])
    out = table_payoff.inventory_hit_report(rep)
    assert out["supplied"] == 2          # /a and /c (source == situation)
    assert out["avoided_gap"] == 2
    assert out["silent"] == 1            # only /c
    assert out["prefilled_confirm"] == 1  # /a
    assert out["table_supplied"] == 1
    assert out["rate"] == 1.0


def test_metric_separates_silent_from_confirm():
    """Mutation guard: summing the two would let a collapse in `silent` hide."""
    rep = _Report([_req(schema_path="/a", value_origin="content_derived")])
    out = table_payoff.inventory_hit_report(rep)
    assert out["silent"] == 0 and out["prefilled_confirm"] == 1


def test_metric_reads_per_objective_not_the_ask_bundle():
    """The ask_bundle is a deduped SUBSET that by construction omits silent binds —
    reading it would zero out exactly the numerator the metric measures."""
    silent = _req(schema_path="/c", state="have", value_origin="operator")
    asked = _req(schema_path="/a", value_origin="content_derived")
    rep = _Report([silent, asked], ask=[asked])
    out = table_payoff.inventory_hit_report(rep)
    assert out["supplied"] == 2, "the silent bind was dropped — read per_objective"
    assert out["silent"] == 1


def test_metric_handles_the_snapshot_dict_shape():
    """A resumed report comes back as dicts; a getattr-only read would report zero
    in production while every model-based test passed."""
    rep = _Report([_req(schema_path="/a", table_item_id="t-1")])
    as_dicts = type("R", (), {
        "per_objective": {1: [r.model_dump(mode="json") for r in rep.per_objective[1]]},
        "ask_bundle": [],
    })()
    out = table_payoff.inventory_hit_report(as_dicts)
    assert out["supplied"] == 1 and out["table_supplied"] == 1


def test_metric_is_defensive_on_junk():
    for junk in (None, 12345, "nope", object()):
        out = table_payoff.inventory_hit_report(junk)
        assert out["supplied"] == 0 and out["rate"] == 0.0


def test_metric_rate_is_zero_not_an_error_on_an_empty_run():
    assert table_payoff.inventory_hit_report(_Report([]))["rate"] == 0.0


# ═══ 4. §5.10.c the during-task chip row ═══════════════════════════════════
def test_using_from_table_names_the_items():
    items = [TableItem(id="t-1", kind="tool", name="GitHub push", ref={}),
             TableItem(id="t-2", kind="service", name="Stripe", ref={})]
    rep = _Report([_req(schema_path="/a", table_item_id="t-1"),
                   _req(schema_path="/b", table_item_id="t-2")])
    out = table_payoff.using_from_table(rep, items)
    assert [c["name"] for c in out["chips"]] == ["GitHub push", "Stripe"]


def test_using_from_table_dedupes_by_item():
    """Two leaves answered by ONE item is one chip, not two."""
    items = [TableItem(id="t-1", kind="tool", name="GitHub push", ref={})]
    rep = _Report([_req(schema_path="/a", table_item_id="t-1"),
                   _req(schema_path="/b", table_item_id="t-1")])
    out = table_payoff.using_from_table(rep, items)
    assert out["total"] == 1 and len(out["chips"]) == 1


def test_using_from_table_caps_and_reports_overflow():
    """Capped for the strip, but the overflow is REPORTED — the row must never
    quietly under-state how much of the run leaned on the table."""
    n = table_payoff.MAX_USING_CHIPS + 3
    items = [TableItem(id=f"t-{i}", kind="tool", name=f"n{i}", ref={}) for i in range(n)]
    rep = _Report([_req(schema_path=f"/{i}", table_item_id=f"t-{i}") for i in range(n)])
    out = table_payoff.using_from_table(rep, items)
    assert len(out["chips"]) == table_payoff.MAX_USING_CHIPS
    assert out["overflow"] == 3 and out["total"] == n


def test_using_from_table_keeps_an_item_removed_mid_run():
    """An id with no matching TableItem still yields a chip (under its id), so the
    strip cannot disagree with the metric after a mid-run removal."""
    rep = _Report([_req(schema_path="/a", table_item_id="t-gone")])
    out = table_payoff.using_from_table(rep, [])
    assert out["total"] == 1 and out["chips"][0]["name"] == "t-gone"


def test_using_from_table_ignores_unattributed_binds():
    rep = _Report([_req(schema_path="/a"), _req(schema_path="/b", table_item_id="")])
    assert table_payoff.using_from_table(rep, [])["total"] == 0


# ═══ 5. §5.10.c the completion chip (≤1, novelty-gated) ════════════════════
class _Vault:
    def __init__(self, root):
        self.root = str(root)


# ═══ 5b. the PRODUCTION call site (shadow_runtime._populate_requirement_report) ═══
#
# The counter and the formatter above are helper-level. This repo has repeatedly shipped
# a correct helper that nothing called — a dropped vault arg, a deleted SSRF gate, an
# unregistered stage — each fully green. These tests drive the real producer.
class _ProdCtx:
    def __init__(self):
        self.files_produced = []


def _curated_capability_situation():
    item = TableItem(id="t-1", kind="tool", name="p", ref={"tool_id": "github_push"})
    return _composed_situation(item).model_dump(mode="json")


def _run_producer(monkeypatch, *, punch: bool):
    """Drive the REAL producer, optionally with the IMPL-5 clamp punched. Returns the
    warning strings the runtime emitted."""
    import systemu.runtime.requirement_binder as rb
    import systemu.runtime.shadow_runtime as sr
    from unittest.mock import MagicMock, patch

    if punch:
        monkeypatch.setattr(rb, "_entry_origin", lambda entry: rb._OPERATOR)

    fake_log = MagicMock()
    monkeypatch.setattr(sr, "logger", fake_log)
    with patch("systemu.runtime.elicitation.surface_ask_bundle_requirement",
               return_value={"action": "cancel", "content": {}}):
        sr._populate_requirement_report(
            _ProdCtx(),
            objectives=[Objective(id=1, goal="do it", success_criteria="Done")],
            capability=_tool({"type": "object", "required": ["github_push"],
                              "properties": {"github_push": {"type": "string"}}}),
            situation=_curated_capability_situation(),
        )
    return [str(c.args) for c in fake_log.warning.call_args_list]


def test_the_producer_fires_the_tripwire_on_a_punched_clamp(monkeypatch):
    """The wire is ATTACHED in production, not merely importable."""
    warnings = _run_producer(monkeypatch, punch=True)
    assert any("CLAMP REGRESSION" in w for w in warnings), warnings


def test_the_producer_is_quiet_on_a_healthy_clamp(monkeypatch):
    """Discriminator: a producer that warned unconditionally would pass the test
    above while measuring nothing."""
    warnings = _run_producer(monkeypatch, punch=False)
    assert not any("CLAMP REGRESSION" in w for w in warnings), warnings


def test_the_tripwire_runs_BEFORE_the_empty_ask_early_return(monkeypatch):
    """ORDERING pin — the mutation this whole wiring is most likely to lose to.

    ``_populate_requirement_report`` returns early when the ask_bundle is empty. A
    table-sourced silent bind is BY DEFINITION not in the ask_bundle, so a fully
    punched clamp produces an EMPTY bundle — the worst case is precisely the case the
    early return would skip. This asserts the fired warning coexists with an empty
    bundle and an unstashed report, which is only possible if the check runs first.
    """
    import systemu.runtime.requirement_binder as rb
    import systemu.runtime.shadow_runtime as sr
    from unittest.mock import MagicMock, patch

    monkeypatch.setattr(rb, "_entry_origin", lambda entry: rb._OPERATOR)

    # confirm the premise: with the clamp punched, this run asks for NOTHING.
    report = rb.build_requirement_report(
        [Objective(id=1, goal="do it", success_criteria="Done")],
        _tool({"type": "object", "required": ["github_push"],
               "properties": {"github_push": {"type": "string"}}}),
        _curated_capability_situation(), _ProdCtx())
    assert report.ask_bundle == [], (
        "premise broken: this fixture must produce an EMPTY ask_bundle for the "
        "ordering pin to mean anything"
    )

    fake_log = MagicMock()
    monkeypatch.setattr(sr, "logger", fake_log)
    ctx = _ProdCtx()
    with patch("systemu.runtime.elicitation.surface_ask_bundle_requirement") as _surface:
        sr._populate_requirement_report(
            ctx, objectives=[Objective(id=1, goal="do it", success_criteria="Done")],
            capability=_tool({"type": "object", "required": ["github_push"],
                              "properties": {"github_push": {"type": "string"}}}),
            situation=_curated_capability_situation(),
        )

    warnings = [str(c.args) for c in fake_log.warning.call_args_list]
    assert any("CLAMP REGRESSION" in w for w in warnings), (
        "the tripwire was moved AFTER the empty-ask early return — it is now blind "
        "to a fully-punched clamp, the exact case it exists for"
    )
    # and the AC6 no-op still holds: nothing stashed, nothing surfaced.
    assert getattr(ctx, "_requirement_report", None) is None
    _surface.assert_not_called()


def test_the_tripwire_does_not_perturb_the_snapshot_on_a_healthy_run(monkeypatch):
    """AC6: the wire is READ-ONLY. It must not stash anything on the context."""
    import systemu.runtime.shadow_runtime as sr
    from unittest.mock import patch

    ctx = _ProdCtx()
    before = set(vars(ctx))
    with patch("systemu.runtime.elicitation.surface_ask_bundle_requirement",
               return_value={"action": "cancel", "content": {}}):
        sr._populate_requirement_report(
            ctx, objectives=[Objective(id=1, goal="do it", success_criteria="Done")],
            capability=_tool({"type": "object", "required": ["github_push"],
                              "properties": {"github_push": {"type": "string"}}}),
            situation=_curated_capability_situation(),
        )
    assert set(vars(ctx)) - before <= {"_requirement_report"}, (
        "the tripwire stashed state on the context — it must be read-only"
    )


def test_the_completion_chip_is_one_chip_for_many_binds(tmp_path):
    """§5.10.c: ≤1 aggregated chip per task, never one per bind."""
    v = _Vault(tmp_path)
    rep = _Report([_req(schema_path=f"/{i}", table_item_id="t-1") for i in range(5)])
    out = table_payoff.answered_from_table(rep, v)
    assert out["count"] == 5
    assert isinstance(out["chip"], str) and out["chip"].count("answered") == 1


def test_the_novelty_gate_stops_celebrating(tmp_path):
    v = _Vault(tmp_path)
    rep = _Report([_req(schema_path="/a", table_item_id="t-1")])
    for _ in range(table_payoff.NOVELTY_CELEBRATION_LIMIT):
        assert table_payoff.answered_from_table(rep, v)["chip"] is not None
    assert table_payoff.answered_from_table(rep, v)["chip"] is None


def test_the_raw_count_survives_suppression(tmp_path):
    """§5.10.c is explicit: the raw count still feeds §10 when the chip is gated.
    A caller reading only `chip` would silently under-count the metric."""
    v = _Vault(tmp_path)
    rep = _Report([_req(schema_path="/a", table_item_id="t-1")])
    for _ in range(table_payoff.NOVELTY_CELEBRATION_LIMIT):
        table_payoff.answered_from_table(rep, v)
    out = table_payoff.answered_from_table(rep, v)
    assert out["chip"] is None and out["suppressed"] is True
    assert out["count"] == 1, "the raw count was lost when the chip was suppressed"


def test_a_novel_item_revives_the_chip_in_an_otherwise_familiar_run(tmp_path):
    """The gate is per ITEM. A brand-new item is exactly what is worth teaching,
    even when everything else in the run is familiar."""
    v = _Vault(tmp_path)
    old = _Report([_req(schema_path="/a", table_item_id="t-old")])
    for _ in range(table_payoff.NOVELTY_CELEBRATION_LIMIT):
        table_payoff.answered_from_table(old, v)
    assert table_payoff.answered_from_table(old, v)["chip"] is None

    mixed = _Report([_req(schema_path="/a", table_item_id="t-old"),
                     _req(schema_path="/b", table_item_id="t-new")])
    assert table_payoff.answered_from_table(mixed, v)["chip"] is not None


def test_record_false_does_not_burn_novelty(tmp_path):
    """A preview/refresh must not consume an item's novelty budget."""
    v = _Vault(tmp_path)
    rep = _Report([_req(schema_path="/a", table_item_id="t-1")])
    for _ in range(table_payoff.NOVELTY_CELEBRATION_LIMIT + 3):
        assert table_payoff.answered_from_table(rep, v, record=False)["chip"] is not None
    assert table_payoff.load_celebrations(v) == {}


def test_no_chip_when_nothing_came_from_the_table(tmp_path):
    v = _Vault(tmp_path)
    rep = _Report([_req(schema_path="/a")])
    out = table_payoff.answered_from_table(rep, v)
    assert out["count"] == 0 and out["chip"] is None and out["suppressed"] is False


def test_a_missing_gap_is_not_celebrated(tmp_path):
    """An item that was on the table but still could not fill the leaf is not a win."""
    v = _Vault(tmp_path)
    rep = _Report([_req(schema_path="/a", state="missing", table_item_id="t-1")])
    assert table_payoff.answered_from_table(rep, v)["count"] == 0


def test_the_celebration_ledger_is_bounded(tmp_path):
    v = _Vault(tmp_path)
    n = table_payoff.MAX_NOVELTY_KEYS + 25
    rep = _Report([_req(schema_path=f"/{i}", table_item_id=f"t-{i}") for i in range(n)])
    table_payoff.answered_from_table(rep, v)
    assert len(table_payoff.load_celebrations(v)) <= table_payoff.MAX_NOVELTY_KEYS


def test_a_broken_ledger_degrades_to_celebrating(tmp_path):
    v = _Vault(tmp_path)
    from systemu.runtime.table_store import _dir
    _dir(v).mkdir(parents=True, exist_ok=True)
    (_dir(v) / "table_celebrations.json").write_text("{not json", encoding="utf-8")
    rep = _Report([_req(schema_path="/a", table_item_id="t-1")])
    assert table_payoff.answered_from_table(rep, v)["chip"] is not None


def test_no_vault_renders_without_persisting():
    rep = _Report([_req(schema_path="/a", table_item_id="t-1")])
    out = table_payoff.answered_from_table(rep, None)
    assert out["chip"] is not None and out["count"] == 1
