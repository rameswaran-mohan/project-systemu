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


def test_is_ask_tracks_the_binder():
    """table_payoff._is_ask is a re-implementation (it must read dicts too). Pin it
    against the binder's own predicate so the two cannot drift."""
    for state in ("have", "resolvable", "missing"):
        for origin in ("operator", "systemu_authored", "content_derived"):
            r = _req(state=state, value_origin=origin)
            assert table_payoff._is_ask(r) == _needs_ask(r), (state, origin)
            # and the dict shape a resumed snapshot produces
            assert table_payoff._is_ask(r.model_dump(mode="json")) == _needs_ask(r)


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
