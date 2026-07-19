"""R-B3 / T3 — the "Set the table" consult + the bounded ``table_propose``
(spec UNIFIED-v2 §5.10.1, bounds from §5.10.b#2/#5/#6).

The five bounds the plan slot names, each pinned here:

  1. ``table_propose`` bounds — cannot modify an ``operator_added`` item; a
     NON-consult context lands ``suggested`` + ``content_derived``.
  2. Commit-review is required before ANY item lands.
  3. Declared-by-default.
  4. Provider-absent path — the deterministic palette stays fully usable.
  5. Cap interaction — the consult burns ZERO harness-request budget (no
     ``HarnessRequest`` is ever constructed).

Plus the standing invariants this surface has already been bitten by: taint never
launders, tombstones are respected (including the ``ref_key("tool", …)`` trap),
``suggested`` is never auto-promoted, and no secret reaches an item.

Deliberately free of ``inspect.getsource`` so the whole module stays in the
EDIT-SAFE tier (``pytest -m "not source_sensitive"``, conftest GATE-TIER).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.runtime import table_store as ts
from systemu.runtime import table_reconciler as tr
from systemu.runtime import table_consult as tc


# --------------------------------------------------------------------------- #
# fakes — same shape as tests/test_onthetable.py (the shipped T1 fixture): the
# reconciler only ever touches ``.root`` and ``.list_tools()``.
# --------------------------------------------------------------------------- #

class _Vault:
    def __init__(self, root: Path, tools=None):
        self.root = str(root)
        self._tools = tools or []

    def list_tools(self, status=None):
        return list(self._tools)


def _keys(items):
    return {ts.ref_key(i.kind, i.ref) for i in items}


def _by_name(items, name):
    for i in items:
        if i.name == name:
            return i
    return None


def _reviewed_session(**kw):
    s = tc.ConsultSession(**kw)
    s.reviewed = True
    return s


# --------------------------------------------------------------------------- #
# BOUND 1a — table_propose can never touch an operator_added item
# --------------------------------------------------------------------------- #

def test_propose_cannot_modify_an_operator_added_item(tmp_path):
    v = _Vault(tmp_path)
    ts.add_operator_item(v, ts.make_operator_item("service", "Gmail", "my mail"))

    res = tc.propose(v, kind="service", name="Gmail", detail="hijacked")

    assert res["accepted"] is False
    assert res["reason"] == "operator_added"
    # the operator's own card is untouched, in EVERY field a proposal could have moved
    op = ts.load_operator_items(v)
    assert len(op) == 1
    assert (op[0].provenance, op[0].origin_class, op[0].status, op[0].detail) == \
           ("operator_added", "operator", "declared", "my mail")
    # ...and nothing was written to the proposal sidecar behind its back
    assert ts.load_proposed_items(v) == []


def test_propose_is_create_only_even_for_its_own_prior_proposal(tmp_path):
    v = _Vault(tmp_path)
    assert tc.propose(v, kind="service", name="Notion", detail="first")["accepted"] is True
    again = tc.propose(v, kind="service", name="Notion", detail="second")

    assert again["accepted"] is False and again["reason"] == "duplicate"
    rows = ts.load_proposed_items(v)
    assert len(rows) == 1 and rows[0].detail == "first"   # never updated in place


# --------------------------------------------------------------------------- #
# BOUND 1b — a NON-consult context lands suggested + content_derived
# --------------------------------------------------------------------------- #

def test_the_proposal_constructor_stamps_untrusted(tmp_path):
    """Pinned at the CONSTRUCTOR, separately from the round-trip below. The loader
    clamps on read, so a wrong stamp here is invisible through the store — and a
    guard no test can kill is a guard that rots."""
    it = ts.make_proposed_item("service", "Acme CRM", "from a web page")
    assert it.status == "suggested"
    assert it.origin_class == "content_derived"
    assert it.provenance == "proposed"


def test_non_consult_context_lands_suggested_and_content_derived(tmp_path):
    v = _Vault(tmp_path)
    res = tc.propose(v, kind="service", name="Acme CRM", detail="from a web page")
    assert res["accepted"] is True

    row = ts.load_proposed_items(v)[0]
    assert row.status == "suggested"
    assert row.origin_class == "content_derived"
    assert row.provenance == "proposed"

    # and it reaches the board with exactly those values (the projector is the
    # only thing the /table page reads)
    projected = _by_name(tr.project(v), "Acme CRM")
    assert projected is not None
    assert (projected.status, projected.origin_class, projected.provenance) == \
           ("suggested", "content_derived", "proposed")


def test_the_registry_tool_has_no_consult_channel_at_all(tmp_path):
    """The forced-provenance bound is STRUCTURAL, not a runtime string check: the
    agent-callable tool's schema exposes no way to name a consult context, so a
    task cannot spell one."""
    from systemu.runtime.tools import table_tools

    props = set(table_tools.TABLE_PROPOSE_SCHEMA["properties"])
    assert props == {"kind", "name", "detail"}
    for forbidden in ("session", "consult", "context", "provenance", "origin_class",
                      "status", "id"):
        assert forbidden not in props


def test_the_registry_tool_writes_an_untrusted_item(tmp_path, monkeypatch):
    from systemu.runtime.tools import table_tools

    v = _Vault(tmp_path)
    monkeypatch.setattr(table_tools, "_open_vault", lambda: v)
    out = table_tools.table_propose_handler(kind="service", name="Acme CRM")

    assert out["success"] is True and out["accepted"] is True
    row = ts.load_proposed_items(v)[0]
    assert (row.status, row.origin_class, row.provenance) == \
           ("suggested", "content_derived", "proposed")


# --------------------------------------------------------------------------- #
# BOUND 2 — commit-review is required before ANY item lands
# --------------------------------------------------------------------------- #

def test_pending_ghosts_do_not_reach_the_table(tmp_path):
    v = _Vault(tmp_path)
    s = tc.ConsultSession()
    tc.stage(s, "services", ["Gmail", "Notion"])

    assert len(s.pending) == 2                       # ghosts exist in the session…
    assert tr.project(v) == []                       # …and nowhere else
    assert ts.load_consulted_items(v) == []


def test_commit_without_review_refuses_and_writes_nothing(tmp_path):
    v = _Vault(tmp_path)
    s = tc.ConsultSession()
    tc.stage(s, "services", ["Gmail"])
    assert s.reviewed is False

    with pytest.raises(tc.ConsultNotReviewed):
        tc.commit(v, s)

    assert ts.load_consulted_items(v) == []
    assert tr.project(v) == []


def test_editing_a_ghost_mid_consult_never_touches_the_store(tmp_path):
    v = _Vault(tmp_path)
    s = tc.ConsultSession()
    tc.stage(s, "services", ["Gmial"])               # typo
    tc.edit_pending(s, 0, name="Gmail")
    tc.stage(s, "services", ["Dropbox"])
    tc.drop_pending(s, 1)

    assert [p.name for p in s.pending] == ["Gmail"]
    assert ts.load_consulted_items(v) == []          # still nothing persisted

    # the EDIT must re-derive ref/id, not just relabel: an item whose ref still
    # said "Gmial" would tombstone, heal and dedup as the wrong thing forever,
    # while every surface showed the corrected name.
    fixed = ts.ref_key("service", {"server": "Gmail"})
    assert ts.ref_key(s.pending[0].kind, s.pending[0].ref) == fixed
    assert s.pending[0].id == ts.id_for_key(fixed)

    s.reviewed = True
    assert tc.commit(v, s) == 1
    stored = ts.load_consulted_items(v)[0]
    assert stored.name == "Gmail"
    assert ts.ref_key(stored.kind, stored.ref) == fixed


def test_review_lines_name_every_pending_item_before_commit(tmp_path):
    s = tc.ConsultSession()
    tc.stage(s, "services", ["Gmail"])
    tc.stage(s, "data", ["C:/Users/me/Invoices"])
    lines = tc.review_lines(s)
    assert len(lines) == 2
    assert any("Gmail" in ln for ln in lines)
    assert any("Invoices" in ln for ln in lines)


def test_commit_is_consumed_so_a_second_click_cannot_relaunder(tmp_path):
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "services", ["Gmail"])
    assert tc.commit(v, s) == 1
    assert s.reviewed is False and s.pending == []
    with pytest.raises(tc.ConsultNotReviewed):
        tc.commit(v, s)


# --------------------------------------------------------------------------- #
# BOUND 3 — declared-by-default
# --------------------------------------------------------------------------- #

def test_every_committed_consult_item_is_declared_and_operator(tmp_path):
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "services", ["Gmail"])
    tc.stage(s, "mcp_servers", ["https://mcp.example.com/sse"])
    tc.stage(s, "data", ["C:/Users/me/Invoices"])
    tc.stage(s, "credentials", ["openrouter"])
    tc.stage(s, "preferences", ["reports as PDF"])
    tc.commit(v, s)

    rows = ts.load_consulted_items(v)
    assert len(rows) == 5
    for r in rows:
        assert r.status == "declared", f"{r.name} did not land declared"
        assert r.provenance == "consulted"
        assert r.origin_class == "operator"       # §5.10.b#7 — operator-typed
    # nothing was configured as a side effect (declare-now-configure-later)
    assert not any(r.status in ("ready", "configuring") for r in tr.project(v))


def test_a_consulted_item_heals_to_the_live_object_instead_of_duplicating(tmp_path):
    """Declared-by-default only works if the declaration and the later real thing
    key identically — otherwise the operator gets two cards forever."""
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "mcp_servers", ["https://mcp.example.com/sse"])
    tc.commit(v, s)
    assert [i.status for i in tr.project(v)] == ["declared"]

    # the server now really exists
    import systemu.runtime.mcp.connections as conns
    _saved = (conns.all_servers, conns.enabled_tools, conns.is_server_connected)
    try:
        conns.all_servers = lambda _v: ["https://mcp.example.com/sse"]
        conns.enabled_tools = lambda _v: []
        conns.is_server_connected = lambda _v, _s: True
        projected = tr.project(v)
    finally:
        conns.all_servers, conns.enabled_tools, conns.is_server_connected = _saved

    assert len(projected) == 1                      # ONE card, not two
    assert projected[0].status == "ready"
    assert projected[0].provenance == "migrated"


# --------------------------------------------------------------------------- #
# BOUND 4 — the provider-absent path
# --------------------------------------------------------------------------- #

def test_consult_is_gated_on_a_configured_provider():
    assert tc.consult_available(provider_configured=True) is True
    assert tc.consult_available(provider_configured=False) is False


def test_empty_state_leads_with_the_palette_when_no_provider():
    cta = tc.empty_state_cta(provider_configured=False)
    assert cta["primary"] == "put_on_the_table"
    assert "connect a model" in cta["note"].lower()
    assert tc.empty_state_cta(provider_configured=True)["primary"] == "set_the_table"


def test_the_palette_is_fully_usable_with_no_provider(tmp_path, monkeypatch):
    """Provider-absent must degrade the CONSULT only. The deterministic declare
    path — the whole point of the fallback — has to work end to end."""
    import systemu.runtime.platform_profile as pp
    monkeypatch.setattr(pp, "_provider_configured", lambda: False)
    assert tc.consult_available() is False

    v = _Vault(tmp_path)
    for kind, name in (("service", "Gmail"), ("data_root", "C:/Invoices"),
                       ("credential_ref", "openrouter")):
        ts.add_operator_item(v, ts.make_operator_item(kind, name))

    projected = tr.project(v)
    assert len(projected) == 3
    assert all(i.provenance == "operator_added" and i.status == "declared"
               for i in projected)


def test_staging_degrades_to_a_deterministic_parse_when_the_llm_is_absent():
    """No provider ⇒ no LLM parse. The area still yields items from the raw text
    rather than dying, so a half-configured install is never a dead end."""
    items = tc.parse_area_answers(
        "services", {"items": "Gmail, Notion\nDropbox"}, llm_fn=None, config=None)
    assert [i["name"] for i in items] == ["Gmail", "Notion", "Dropbox"]


def test_the_llm_parse_is_used_when_available_and_cannot_choose_the_kind():
    """The LLM only extracts NAMES from free text; the KIND is fixed by the
    coverage area. An LLM that tries to pick a kind is ignored — that is what
    keeps a parse from minting a `tool` card (the ref_key trap) or a posture one."""
    calls = []

    def _fake_llm(**kw):
        calls.append(kw)
        return {"items": [{"name": "Gmail", "detail": "work mail",
                           "kind": "tool"}]}

    items = tc.parse_area_answers(
        "services", {"items": "the google mail thing"}, llm_fn=_fake_llm,
        config=object())
    assert len(calls) == 1
    assert items == [{"kind": "service", "name": "Gmail", "detail": "work mail"}]


def test_a_broken_llm_parse_falls_back_rather_than_raising():
    def _boom(**kw):
        raise RuntimeError("provider died mid-consult")

    items = tc.parse_area_answers(
        "services", {"items": "Gmail"}, llm_fn=_boom, config=object())
    assert [i["name"] for i in items] == ["Gmail"]


def test_the_first_run_banner_needs_a_provider(tmp_path):
    v = _Vault(tmp_path)
    assert tc.should_show_first_run_banner(v, True) is True
    assert tc.should_show_first_run_banner(v, False) is False


def test_the_first_run_banner_stops_once_the_operator_has_declared_anything(tmp_path):
    """A table full of MIGRATED cards is still cold — the operator has told
    systemu nothing. A declaration or a consult answer is what ends first run."""
    v = _Vault(tmp_path)
    assert tc.should_show_first_run_banner(v, True) is True
    ts.add_operator_item(v, ts.make_operator_item("service", "Gmail"))
    assert tc.should_show_first_run_banner(v, True) is False

    v2 = _Vault(tmp_path / "b")
    s = _reviewed_session()
    tc.stage(s, "services", ["Notion"])
    tc.commit(v2, s)
    assert tc.should_show_first_run_banner(v2, True) is False


def test_the_first_run_banner_is_dismissible_for_good(tmp_path):
    v = _Vault(tmp_path)
    assert tc.should_show_first_run_banner(v, True) is True
    tc.dismiss_first_run_banner(v)
    assert tc.should_show_first_run_banner(v, True) is False


def test_the_banner_dismissal_is_not_an_inventory_row(tmp_path):
    """Its marker must not project onto the board as a card."""
    v = _Vault(tmp_path)
    tc.dismiss_first_run_banner(v)
    assert tr.project(v) == []


# --------------------------------------------------------------------------- #
# BOUND 5 — the consult burns ZERO harness-request budget
# --------------------------------------------------------------------------- #

def test_the_whole_consult_emits_no_harness_request(tmp_path, monkeypatch):
    """§5.10.1 cap-interaction note: the consult does not ride the ReAct/harness
    path at all. Pinned by counting HarnessRequest CONSTRUCTIONS across a full
    run of the flow — the only way budget is ever spent."""
    import systemu.core.models as models

    built = []
    _orig = models.HarnessRequest.__init__

    def _spy(self, *a, **kw):
        built.append(kw or a)
        _orig(self, *a, **kw)

    monkeypatch.setattr(models.HarnessRequest, "__init__", _spy)

    v = _Vault(tmp_path)
    s = tc.ConsultSession()
    for area in tc.area_ids():
        tc.area_schema(area)                        # render the form
        parsed = tc.parse_area_answers(area, {"items": "Thing One"},
                                       llm_fn=None, config=None)
        tc.stage_parsed(s, area, parsed)
    tc.review_lines(s)
    s.reviewed = True
    tc.commit(v, s)
    tr.project(v)
    tc.propose(v, kind="service", name="From A Task")

    assert built == [], f"the consult spent harness budget: {built}"


def test_the_consult_module_does_not_import_the_react_runtime():
    """A second, structural half of the same bound: if the consult never reaches
    the harness runtime it cannot be force-terminated by a request cap either.

    Run in a SUBPROCESS — evicting ``shadow_runtime`` from this interpreter's
    ``sys.modules`` to measure it would re-run its module-level setup for every
    later test in the session."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "import systemu.runtime.table_consult\n"
        "import systemu.runtime.tools.table_tools\n"
        "print('shadow_runtime' if 'systemu.runtime.shadow_runtime' in sys.modules"
        " else 'clean')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(Path(__file__).resolve().parent.parent), timeout=300)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "clean", r.stdout


# --------------------------------------------------------------------------- #
# standing invariants — taint, tombstones, no-auto-promote, secrets
# --------------------------------------------------------------------------- #

def test_the_proposal_sidecar_cannot_launder_taint(tmp_path):
    """A hand-edited (or task-poisoned) proposal file claiming operator trust is
    clamped unconditionally — everything in this file is by definition NOT an
    operator declaration, which is exactly why it is its own file."""
    v = _Vault(tmp_path)
    (tmp_path / "table").mkdir(parents=True)
    (tmp_path / "table" / "proposed_items.json").write_text(json.dumps([{
        "id": "ti_forged", "kind": "service", "name": "Evil",
        "status": "ready", "provenance": "operator_added",
        "origin_class": "operator", "ref": {"server": "Evil"},
    }]), encoding="utf-8")

    row = ts.load_proposed_items(v)[0]
    assert row.provenance == "proposed"
    assert row.origin_class == "content_derived"
    assert row.status == "suggested"


def test_propose_never_reuses_the_operator_stamp(tmp_path):
    """The operator sidecar's force-stamp is the anti-forgery boundary for
    operator trust. A proposal must not land in that file at all."""
    v = _Vault(tmp_path)
    tc.propose(v, kind="service", name="Acme CRM")
    assert ts.load_operator_items(v) == []
    assert not (tmp_path / "table" / "operator_items.json").exists()


def test_propose_refuses_the_tool_kind_ref_key_trap(tmp_path):
    """``ref_key("tool", …)`` prefers ``tool_id``, so a removal tombstones
    ``tool:<tool_id>`` while a name-derived proposal keys ``tool:<name>`` — the
    keys never meet and the operator's deletion is silently defeated. Refused
    until a name→tool_id resolver exists."""
    v = _Vault(tmp_path)
    res = tc.propose(v, kind="tool", name="zipper")
    assert res["accepted"] is False and res["reason"] == "kind_not_allowed"
    assert ts.load_proposed_items(v) == []


def test_the_consult_cannot_stage_a_tool_either(tmp_path):
    assert "tool" not in {a["kind"] for a in tc.AREAS}


def test_propose_respects_a_tombstone(tmp_path):
    v = _Vault(tmp_path)
    ts.add_tombstone(v, ts.ref_key("service", {"server": "Acme CRM"}))
    res = tc.propose(v, kind="service", name="Acme CRM")
    assert res["accepted"] is False and res["reason"] == "tombstoned"
    assert ts.load_proposed_items(v) == []


def test_a_tombstone_written_after_a_proposal_still_hides_it(tmp_path):
    """Read-side half: a sidecar row written before the removal must not survive it."""
    v = _Vault(tmp_path)
    tc.propose(v, kind="service", name="Acme CRM")
    ts.add_tombstone(v, ts.ref_key("service", {"server": "Acme CRM"}))
    assert _by_name(tr.project(v), "Acme CRM") is None


def test_a_tombstone_written_after_a_consult_commit_hides_it(tmp_path):
    """The mirror of the override below, and the half that actually protects the
    operator: `add_consulted_item` CLEARS a tombstone, so without a read-side
    check a later removal of a consulted card would be silently undone."""
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "services", ["Gmail"])
    tc.commit(v, s)
    assert _by_name(tr.project(v), "Gmail") is not None

    ts.add_tombstone(v, ts.ref_key("service", {"server": "Gmail"}))
    assert _by_name(tr.project(v), "Gmail") is None


def test_the_consult_sidecar_stamps_are_not_forgeable(tmp_path):
    """A hand-edited consult file cannot collect an ``operator_added`` badge it
    never earned, nor paint a healthy status on something never configured."""
    v = _Vault(tmp_path)
    (tmp_path / "table").mkdir(parents=True)
    (tmp_path / "table" / "consulted_items.json").write_text(json.dumps([{
        "id": "ti_forged", "kind": "service", "name": "Evil",
        "status": "ready", "provenance": "operator_added",
        "origin_class": "content_derived", "ref": {"server": "Evil"},
    }]), encoding="utf-8")

    row = ts.load_consulted_items(v)[0]
    assert row.provenance == "consulted"
    assert row.origin_class == "operator"
    assert row.status == "declared"


def test_a_consult_declaration_overrides_a_tombstone(tmp_path):
    """Mirrors ``add_operator_item``: re-declaring X in the consult is a DIRECT
    operator action and means the operator wants X back."""
    v = _Vault(tmp_path)
    ts.add_tombstone(v, ts.ref_key("service", {"server": "Gmail"}))
    s = _reviewed_session()
    tc.stage(s, "services", ["Gmail"])
    tc.commit(v, s)
    assert _by_name(tr.project(v), "Gmail") is not None


def test_a_proposal_is_never_auto_promoted(tmp_path):
    v = _Vault(tmp_path)
    tc.propose(v, kind="service", name="Acme CRM")
    for _ in range(3):
        tr.reconcile_once(v)
    row = _by_name(ts.load_items(v), "Acme CRM")
    assert row is not None and row.status == "suggested"
    assert row.origin_class == "content_derived"


def test_the_projector_and_the_store_derive_the_same_item_id(tmp_path):
    """THE mechanism behind every "one card, not two" claim on this surface: a
    sidecar item and the live/declared card it duplicates collapse because both
    ids derive from the shared ``ref_key`` by the same function. Nothing else in
    the merge enforces precedence, so this is what has to hold."""
    key = ts.ref_key("service", {"server": "Acme CRM"})
    assert tr._id_for(key) == ts.id_for_key(key)


def test_an_operator_declaration_outranks_a_proposal_for_the_same_thing(tmp_path):
    v = _Vault(tmp_path)
    tc.propose(v, kind="service", name="Acme CRM", detail="guessed")
    ts.add_operator_item(v, ts.make_operator_item("service", "Acme CRM", "mine"))
    projected = [i for i in tr.project(v) if i.name == "Acme CRM"]
    assert len(projected) == 1
    assert projected[0].provenance == "operator_added"
    assert projected[0].origin_class == "operator"


@pytest.mark.parametrize("value", [
    "postgres://admin:pw@db/prod",
    "sk-abcdef0123456789abcdef0123456789",
    "--token hunter2",
])
def test_no_secret_can_ride_a_proposal(tmp_path, value):
    v = _Vault(tmp_path)
    assert tc.propose(v, kind="service", name=value)["reason"] == "secret"
    assert tc.propose(v, kind="service", name="ok", detail=value)["reason"] == "secret"
    assert ts.load_proposed_items(v) == []


@pytest.mark.parametrize("value", ["Gmail", "C:/Users/me/Invoices", "Asia/Kolkata"])
def test_ordinary_values_are_not_mistaken_for_secrets(tmp_path, value):
    """The negative control: a blanket refusal would silently disable the slice."""
    v = _Vault(tmp_path)
    assert tc.propose(v, kind="service", name=value)["accepted"] is True


def test_no_secret_can_ride_a_consult_commit(tmp_path):
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "services", ["postgres://admin:pw@db/prod", "Gmail"])
    tc.commit(v, s)
    names = [i.name for i in ts.load_consulted_items(v)]
    assert names == ["Gmail"]


def test_a_credential_declaration_carries_no_free_text_note(tmp_path):
    """§5.10.b#6 — no note on a Keys-zone item, so a value cannot be parked there."""
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "credentials", ["openrouter"], detail="the key is hunter2")
    tc.commit(v, s)
    assert ts.load_consulted_items(v)[0].detail == ""


def test_a_posture_preference_can_never_arrive_from_a_task(tmp_path):
    """§5.10.b#5 — posture items can NEVER arrive as ``suggested``/``learned``;
    a friction-decreasing change is danger-gated and must not get a one-click path."""
    v = _Vault(tmp_path)
    for name in ("approval band", "autonomy posture", "auto-allow risky things"):
        res = tc.propose(v, kind="preference", name=name)
        assert res["accepted"] is False and res["reason"] == "posture"
    assert ts.load_proposed_items(v) == []


def test_the_consult_may_declare_posture_but_only_as_intent(tmp_path):
    """The operator saying it in the consult is fine — it lands DECLARED and
    confers nothing (§5.10.b#3 "the table never authorizes")."""
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "posture", ["ask before sending email"])
    tc.commit(v, s)
    row = ts.load_consulted_items(v)[0]
    assert row.kind == "preference" and row.status == "declared"
    assert tc.posture_deep_link() != ""


# --------------------------------------------------------------------------- #
# bounds on volume + progress / resume / re-run diff
# --------------------------------------------------------------------------- #

def test_the_proposal_tray_has_a_standing_cap(tmp_path):
    v = _Vault(tmp_path)
    for n in range(ts.MAX_PROPOSED_ITEMS):
        assert tc.propose(v, kind="service", name=f"svc{n}")["accepted"] is True
    over = tc.propose(v, kind="service", name="one too many")
    assert over["accepted"] is False and over["reason"] == "capped"
    assert len(ts.load_proposed_items(v)) == ts.MAX_PROPOSED_ITEMS


def test_a_consult_session_is_capped(tmp_path):
    s = tc.ConsultSession()
    tc.stage(s, "services", [f"svc{n}" for n in range(tc.MAX_PENDING_PER_SESSION + 5)])
    assert len(s.pending) == tc.MAX_PENDING_PER_SESSION


def test_progress_reports_areas_covered():
    s = tc.ConsultSession()
    assert tc.progress(s) == (0, len(tc.AREAS))
    tc.stage(s, "services", ["Gmail"])
    assert tc.progress(s) == (1, len(tc.AREAS))
    assert tc.next_area(s) == "mcp_servers"


def test_skipping_an_area_still_advances_progress():
    s = tc.ConsultSession()
    tc.stage(s, "services", [])            # operator skipped it
    assert tc.progress(s)[0] == 1
    assert tc.next_area(s) == "mcp_servers"


def test_abandoning_persists_committed_items_only(tmp_path):
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "services", ["Gmail"])
    tc.commit(v, s)
    tc.stage(s, "data", ["C:/Invoices"])   # abandoned here — never reviewed
    del s

    assert [i.name for i in ts.load_consulted_items(v)] == ["Gmail"]
    assert _by_name(tr.project(v), "C:/Invoices") is None


def test_a_rerun_only_asks_about_gaps(tmp_path):
    v = _Vault(tmp_path)
    s = _reviewed_session()
    tc.stage(s, "services", ["Gmail"])
    tc.commit(v, s)

    fresh = tc.ConsultSession()
    remaining = tc.uncovered_areas(v, fresh)
    assert "services" not in remaining
    assert "data" in remaining
    assert tc.next_area(fresh) == remaining[0]


def test_the_consult_button_says_resume_once_the_table_is_partly_set(tmp_path):
    from systemu.interface.pages.table import consult_button_label

    v = _Vault(tmp_path)
    assert consult_button_label(v) == "Set the table"

    s = _reviewed_session()
    tc.stage(s, "services", ["Gmail"])
    tc.commit(v, s)
    assert consult_button_label(v) == "Resume setting the table"


def test_the_area_form_schema_is_the_shipped_requested_schema_shape(tmp_path):
    """§5.10.1 reuses "the existing requested_schema form path", so the areas must
    produce what that renderer consumes — and no field may be REQUIRED, or an
    operator who uses none of a category could not say so and the consult would
    stop being skippable."""
    from systemu.runtime.elicitation import validate_against_schema

    for area_id in tc.area_ids():
        schema = tc.area_schema(area_id)
        assert schema["type"] == "object"
        assert "items" in schema["properties"]
        assert schema["required"] == []
        for spec in schema["properties"].values():
            assert spec["type"] == "string"
            assert spec.get("description")
        assert validate_against_schema(schema, {}) == []      # skippable


def test_an_unknown_area_yields_an_empty_schema_not_a_crash():
    assert tc.area_schema("no_such_area")["properties"] == {}
    assert tc.parse_area_answers("no_such_area", {"items": "x"},
                                 llm_fn=None, config=None) == []


def test_a_rerun_drops_a_restaged_duplicate_at_commit(tmp_path):
    v = _Vault(tmp_path)
    first = _reviewed_session()
    tc.stage(first, "services", ["Gmail"])
    tc.commit(v, first)

    second = _reviewed_session()
    tc.stage(second, "services", ["Gmail", "Notion"])
    assert tc.commit(v, second) == 1
    assert sorted(i.name for i in ts.load_consulted_items(v)) == ["Gmail", "Notion"]
