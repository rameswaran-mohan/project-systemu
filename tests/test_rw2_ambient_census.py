"""R-W2 (W-B) — the WM-7 ambient census (spec §5.11.c, AC5).

§5.11 AC5 has three clauses and each gets its own driven test:

  1. **no census category runs before its grant** — asserted on the PROBE, not on the
     fact count. "Zero facts" is also what a completely broken census produces, so the
     ungranted case is paired with a POSITIVE CONTROL that changes nothing but the
     grant and runs the SAME probe object.
  2. **revoking stops future runs AND purges its facts** — both halves, plus the case
     the purge must NOT touch (a fact with independent evidence).
  3. **a census-discovered capability wins a plan without the operator naming it** —
     driven end-to-end through the REAL ``survey_situation`` and the REAL planner render.

Plus the boundaries R-W2 must not cross: census facts are bind-inert (DEC-26), carry
honest ``content_derived`` provenance (WM-15: an app name IS content), and the census
never reads a credential store.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime import ambient_census as ac
from systemu.runtime import census_consent as cc
from systemu.runtime import situational_inventory as si
from systemu.runtime.world_model import Fact, FactStore, ProvStep, fact_id_for


# ── fixtures: a REAL store + a REAL consent file on disk ─────────────────────

def _vault(tmp_path):
    """The shape both concrete vault types expose. ``FactStore`` reads ``.root`` and
    ``CensusConsentStore`` is constructed from it — verified against ``Vault`` and
    ``FileVault``, which both define ``.root``."""
    return SimpleNamespace(root=tmp_path)


def _spy_probe(values, calls):
    """A probe that RECORDS being called. The consent test's whole point is that an
    ungranted probe is never REACHED, which a fact count cannot distinguish from a probe
    that ran and found nothing."""
    def _probe(limit, budget):
        calls.append(limit)
        return list(values)
    return _probe


def _use_spy(monkeypatch, category, values, calls, kind=None):
    real_kind = kind or ac.PROBES[category][1]
    monkeypatch.setitem(ac.PROBES, category, (_spy_probe(values, calls), real_kind))


def _make_fake_cli(directory: Path, name: str) -> Path:
    """A real, executable-by-``shutil.which`` file, so the PATH probe test drives the
    REAL lookup rather than a stub of it."""
    if os.name == "nt":
        p = directory / f"{name}.bat"
        p.write_text("@echo off\n", encoding="utf-8")
    else:
        p = directory / name
        p.write_text("#!/bin/sh\n", encoding="utf-8")
        p.chmod(0o755)
    return p


def _isolate_cloud_env(monkeypatch, home):
    """Neutralise the REAL machine's cloud-sync signals.

    Not hygiene — the first run of these tests failed because this box genuinely sets
    ``OneDriveConsumer``, and the probe correctly reported it. Every declared location
    variable must be cleared (not just the one a test sets) or the assertion is really
    about the developer's machine.
    """
    for var in ac._CLOUD_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(home)))


def _vault_dirs(tmp_path):
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"):
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")


# ══ AC5 clause 1 — no category runs before its grant ═════════════════════════

def test_an_ungranted_category_never_reaches_its_probe(tmp_path, monkeypatch):
    """The consent gate, asserted where it actually has to hold.

    The two phases run the SAME probe object against the SAME vault; the ONLY difference
    is the grant. So the first phase's empty ``calls`` cannot be explained by a broken
    probe, a broken store, or a broken fact builder — all three are proven working by the
    second phase."""
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    v = _vault(tmp_path)

    ungranted = ac.run_census(v)
    assert calls == [], "an ungranted category's probe must never be reached"
    assert ungranted["skipped"]["installed_apps"] == "not_consented"
    assert ungranted["facts_written"] == 0
    assert FactStore(v).all_facts() == []

    ac.grant_category(v, "installed_apps")                 # the ONLY change
    granted = ac.run_census(v)
    assert calls, "positive control: the same probe must run once consented"
    assert granted["scanned"] == ["installed_apps"]
    assert [f.value for f in FactStore(v).all_facts()] == ["Microsoft Excel"]


def test_every_category_is_gated_not_just_the_one_we_happened_to_test(tmp_path, monkeypatch):
    """The gate is per-category and there is no unguarded category. Runs a spy over
    EVERY declared category with no grants at all."""
    calls: list = []
    for cat in list(ac.PROBES):
        _use_spy(monkeypatch, cat, ["x"], calls)
    summary = ac.run_census(_vault(tmp_path))
    assert calls == []
    assert set(summary["skipped"]) == set(ac.PROBES)
    assert set(summary["skipped"].values()) == {"not_consented"}


def test_a_paused_category_does_not_scan_but_keeps_its_facts(tmp_path, monkeypatch):
    """Pause is the M3 "pause category" surface. It must differ from revoke in exactly
    one way: the facts stay."""
    calls: list = []
    _use_spy(monkeypatch, "path_clis", ["gh"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "path_clis")
    ac.run_census(v)
    assert len(calls) == 1

    assert cc.CensusConsentStore(tmp_path).set_paused("path_clis", True) is True
    later = ac.run_census(v, min_interval_seconds=0)        # due, but paused
    assert len(calls) == 1, "a paused category must not scan"
    assert later["skipped"]["path_clis"] == "not_consented"
    assert [f.value for f in FactStore(v).all_facts()] == ["gh"], \
        "pause is not revoke — the facts stay"

    cc.CensusConsentStore(tmp_path).set_paused("path_clis", False)
    ac.run_census(v, min_interval_seconds=0)
    assert len(calls) == 2, "resuming must scan again"


# ══ AC5 clause 2 — revoking stops future runs AND purges its facts ═══════════

def test_revoking_stops_future_scans_and_purges_the_category_facts(tmp_path, monkeypatch):
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel", "Docker Desktop"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    ac.run_census(v)
    assert len(FactStore(v).all_facts()) == 2
    assert len(calls) == 1

    out = ac.revoke_category(v, "installed_apps")
    assert out["revoked"] is True
    assert out["facts_removed"] == 2 and out["facts_detached"] == 0
    assert FactStore(v).all_facts() == [], "revocation must purge the derived facts"
    assert cc.CensusConsentStore(tmp_path).is_granted("installed_apps") is False

    ac.run_census(v, min_interval_seconds=0)
    assert len(calls) == 1, "revocation must stop FUTURE scans, not just this one"
    assert FactStore(v).all_facts() == []


def test_revocation_purges_only_the_revoked_category(tmp_path, monkeypatch):
    """A second consented category must be untouched — the purge is scoped by the
    provenance ``ref``, not by "everything the census ever wrote"."""
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    _use_spy(monkeypatch, "path_clis", ["gh"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    ac.grant_category(v, "path_clis")
    ac.run_census(v)
    assert len(FactStore(v).all_facts()) == 2

    ac.revoke_category(v, "installed_apps")
    assert [f.value for f in FactStore(v).all_facts()] == ["gh"]
    assert cc.CensusConsentStore(tmp_path).is_granted("path_clis") is True


def test_revocation_keeps_a_fact_that_has_independent_evidence(tmp_path):
    """Revoking a SOURCE is not a statement that the fact is false. A fact the live
    inventory also asserts survives, with only the census step detached — otherwise
    switching the census off would silently delete inventory knowledge."""
    v = _vault(tmp_path)
    store = FactStore(v)
    kind, value = "capability", "excel.write"
    fid = fact_id_for(kind, value)
    store.put_fact(Fact(fact_id=fid, kind=kind, value=value,
                        origin_class="content_derived", confidence=1.0,
                        source_chain=[ProvStep(source_kind=ac.CENSUS_SOURCE_KIND,
                                               ref="installed_apps")]))
    store.put_fact(Fact(fact_id=fid, kind=kind, value=value,
                        origin_class="content_derived", confidence=1.0,
                        source_chain=[ProvStep(source_kind="inventory", ref="excel.write")]))
    assert len(store.get(fid).source_chain) == 2, "precondition: two independent sources"

    out = ac.revoke_category(v, "installed_apps")
    assert out["facts_removed"] == 0 and out["facts_detached"] == 1
    survivor = store.get(fid)
    assert survivor is not None, "a fact with other evidence must survive revocation"
    assert [s.source_kind for s in survivor.source_chain] == ["inventory"]


def test_purge_of_an_unmatched_source_changes_nothing(tmp_path):
    v = _vault(tmp_path)
    store = FactStore(v)
    store.put_fact(Fact(fact_id="service:gh", kind="service", value="github",
                        origin_class="operator",
                        source_chain=[ProvStep(source_kind="inventory", ref="github")]))
    assert store.purge_source_ref("census", "installed_apps") == {"removed": 0,
                                                                 "detached": 0}
    assert [f.fact_id for f in store.all_facts()] == ["service:gh"]


def test_revoke_purges_even_when_the_grant_is_already_gone(tmp_path, monkeypatch):
    """Idempotent cleanup: a fact left behind by a half-completed revocation must still
    be removable, so the operator surface can always be re-run to convergence."""
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    ac.run_census(v)
    # drop ONLY the consent, simulating a purge that never completed
    assert cc.CensusConsentStore(tmp_path).revoke("installed_apps") is True
    assert len(FactStore(v).all_facts()) == 1

    out = ac.revoke_category(v, "installed_apps")
    assert out["revoked"] is False and out["facts_removed"] == 1
    assert FactStore(v).all_facts() == []


def test_consent_revoked_mid_scan_does_not_resurrect_the_facts(tmp_path, monkeypatch):
    """DEC-10 race, reproduced deterministically.

    ``revoke_category`` withdraws consent and THEN purges. A census already past its
    consent check and still probing would otherwise ``put_facts`` after that purge and
    put back exactly what the operator just revoked — a privacy control defeated by
    timing. The probe below revokes from inside itself, which is precisely that
    interleaving with the scheduling removed."""
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")

    def _revoke_while_probing(limit, budget):
        ac.revoke_category(v, "installed_apps")        # the operator revokes mid-probe
        return ["Microsoft Excel"]

    monkeypatch.setitem(ac.PROBES, "installed_apps",
                        (_revoke_while_probing, "installed_application"))
    summary = ac.run_census(v)
    assert summary["discarded"] == ["installed_apps"]
    assert summary["facts_written"] == 0
    assert FactStore(v).all_facts() == [], \
        "a revoked category's facts must not be resurrected by an in-flight scan"


def test_mark_ran_never_creates_a_grant(tmp_path):
    """``mark_ran`` runs on the exec thread after a scan; ``revoke`` runs on the operator
    surface. If ``mark_ran`` could ADD a row, a revoke landing between its load and its
    write would come back as a live grant — consent manufactured by a timestamp."""
    store = cc.CensusConsentStore(tmp_path)
    assert store.mark_ran("installed_apps") is False
    assert store.list_grants() == []
    assert store.is_active("installed_apps") is False


def test_every_consent_mutation_holds_the_rmw_lock(tmp_path, monkeypatch):
    """Every mutator rewrites the WHOLE consent file from its own load, so an unlocked
    RMW lets ``mark_ran`` write a stale snapshot back over a concurrent ``revoke`` and
    RESURRECT the grant — which in turn defeats ``run_census``'s pre-write consent
    re-check. (Reproduced by hand before the lock was added.)

    Asserted at the WRITE rather than by racing threads: a stress test would catch this
    only probabilistically, and dropping the lock from ONE mutator is exactly the change
    a flaky test would miss."""
    store = cc.CensusConsentStore(tmp_path)
    held: list = []
    real_write = cc.CensusConsentStore._write

    def _spy(self, grants):
        held.append(cc._CONSENT_LOCK.locked())
        return real_write(self, grants)

    monkeypatch.setattr(cc.CensusConsentStore, "_write", _spy)
    store.grant("installed_apps")
    store.set_paused("installed_apps", True)
    store.mark_ran("installed_apps")
    store.revoke("installed_apps")
    assert held == [True, True, True, True], \
        "grant / set_paused / mark_ran / revoke must each hold the consent lock"


# ══ AC5 clause 3 — a census capability wins a plan, unnamed by the operator ══

@pytest.mark.asyncio
@pytest.mark.real_survey
async def test_a_census_fact_reaches_the_planner_prompt_without_the_operator_naming_it(
        tmp_path, monkeypatch):
    """The payoff, end-to-end through the REAL survey and the REAL planner render.

    The operator's goal never says "Excel", and no live inventory source can produce an
    ``installed_application`` — so the ONLY way the name reaches the planner prompt is
    census → fact store → ``compose_world_view`` → ``render_situation_for_prompt``.
    ``render_situation_for_prompt`` is called on ``model_dump()`` exactly as
    ``open_world_planner`` calls it in production."""
    from systemu.vault.vault import Vault

    _vault_dirs(tmp_path)
    vault = Vault(str(tmp_path))
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)

    ac.grant_category(vault, "installed_apps")
    assert ac.run_census(vault)["facts_written"] == 1

    scroll = SimpleNamespace(raw_request="put together a budget for next quarter",
                             intent="")
    report, _stamps = await si.survey_situation(scroll, vault=vault)

    live = report.model_dump()
    live.pop("world_facts")
    assert "excel" not in str(live).lower(), \
        "precondition: no LIVE inventory source names Excel — only the census can"
    assert [(r["kind"], r["value"]) for r in report.world_facts] == \
        [("installed_application", "Microsoft Excel")]

    rendered = si.render_situation_for_prompt(report.model_dump())
    assert "Microsoft Excel" in rendered, \
        "the census fact must reach the planner prompt"
    # …and it arrives as FENCED data, not as free-floating prompt text (WM-15).
    assert "untrusted_inventory_data" in rendered


@pytest.mark.asyncio
@pytest.mark.real_survey
async def test_with_no_consent_the_planner_prompt_is_unchanged(tmp_path, monkeypatch):
    """Zero-census operation stays fully functional (WM-7) — and, more precisely, is
    INDISTINGUISHABLE. The same run without a grant must render no census row at all."""
    from systemu.vault.vault import Vault

    _vault_dirs(tmp_path)
    vault = Vault(str(tmp_path))
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)

    assert ac.run_census(vault)["facts_written"] == 0          # no grant
    scroll = SimpleNamespace(raw_request="put together a budget for next quarter",
                             intent="")
    report, _stamps = await si.survey_situation(scroll, vault=vault)
    assert report.world_facts == []
    assert "Microsoft Excel" not in si.render_situation_for_prompt(report.model_dump())


# ══ the boundary R-W2 must NOT cross (DEC-26) ════════════════════════════════

def test_a_census_fact_is_bind_inert(tmp_path, monkeypatch):
    """DEC-26: ``world_facts`` is not a §5.3 bind source, and R-W2 does not make it one.

    Driven through the REAL ``compute_requirements`` with a census-shaped row: a
    situation carrying it must produce bind decisions IDENTICAL to an empty situation.
    Pinned here (not only in R-W1) because the census is the first producer whose facts
    a planner is actually meant to act on — the tempting place to "just let it bind"."""
    from systemu.core.models import Objective, Tool
    from systemu.runtime.requirement_binder import compute_requirements

    calls: list = []
    _use_spy(monkeypatch, "path_clis", ["gh"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "path_clis")
    ac.run_census(v)
    rows = si.compose_world_view(si.SituationReport(), v, "open an issue with gh").world_facts
    assert rows, "precondition: the census row is present in the view"

    empty = {"services": [], "capabilities": [], "roots": [], "credentials": [],
             "profile": {}, "declared_intents": [], "world_facts": []}
    seeded = {**empty, "world_facts": rows}
    tool = Tool(id="t", name="open_issue", description="d", tool_type="python_function",
                parameters_schema={"type": "object",
                                   "properties": {"gh": {"type": "string"}},
                                   "required": ["gh"]})
    obj = Objective(id=1, goal="open an issue with gh", success_criteria="done")
    ctx = SimpleNamespace(_situation_report=None, _granted_roots=None,
                          files_produced=[], vault=None)

    def _decisions(situation):
        return [(r.schema_path, r.state, r.bound_value_ref)
                for r in compute_requirements(obj, tool, situation, ctx)]

    assert _decisions(seeded) == _decisions(empty)
    assert all(state != "have" for _, state, _ in _decisions(seeded))


def test_census_facts_carry_content_derived_provenance(tmp_path, monkeypatch):
    """WM-15: "a filename, an app name, a server description IS content". systemu chose
    to LOOK, but every value it finds was authored by a third party, so the stored
    provenance is the untrusted one — never ``operator``, and not ``systemu_authored``
    either."""
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    ac.run_census(v)
    facts = FactStore(v).all_facts()
    assert facts and all(f.origin_class == "content_derived" for f in facts)
    assert all(f.taint_permits_silent_bind is False for f in facts)
    assert [(s.source_kind, s.ref) for s in facts[0].source_chain] == \
        [(ac.CENSUS_SOURCE_KIND, "installed_apps")]


def test_the_census_writes_no_survey_watermark(tmp_path, monkeypatch):
    """A census watermark would become ``latest_survey()`` and, listing only census
    kinds, would make every INVENTORY fact read ``not_surveyed`` — silently destroying
    the ``unconfirmed`` signal ``goal_view`` drops rows on. The watermark belongs to the
    §5.1 surveyor, which is the only thing that knows what IT covered."""
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    ac.run_census(v)
    assert FactStore(v).all_surveys() == []


# ══ consent cards + the M3 standing-scan disclosure ══════════════════════════

def test_every_probe_has_a_card_and_every_card_has_a_probe():
    """The structural version of "the disclosure is complete". A category that could
    scan without a card would be an undisclosed collection; a card with no probe would
    be a consent request for something that never happens."""
    assert set(ac.PROBES) == set(cc.CATEGORIES)


def test_every_consent_card_discloses_standing_scans_and_what_is_collected():
    for category in cc.CATEGORIES:
        card = cc.consent_card(category)
        assert card["standing_scan"] is True
        assert card["collects"] and all(c.strip() for c in card["collects"])
        assert card["excludes"], "a card must say what it does NOT take"
        notice = card["standing_scan_notice"].lower()
        # the two things M3 requires the ORIGINAL card to say, not merely imply
        assert "re-check" in notice or "re-run" in notice
        assert "revoke" in notice and "delete" in notice


def test_granting_an_unknown_category_is_refused_not_ignored(tmp_path):
    """A typo'd grant that "succeeded" would read to the operator as a live permission
    that quietly scans nothing."""
    v = _vault(tmp_path)
    with pytest.raises(cc.UnknownCensusCategory):
        ac.grant_category(v, "installed_appz")
    assert cc.CensusConsentStore(tmp_path).list_grants() == []
    # revoke is the SAFE direction and must never be blocked by a vocabulary mismatch
    assert ac.revoke_category(v, "installed_appz")["revoked"] is False


def test_an_unknown_category_on_disk_is_dropped_not_honoured(tmp_path):
    """A category removed from the build, or invented by a hand-edit, must not keep
    authorising a scan."""
    (tmp_path / "census_consent.json").write_text(
        '{"version": 1, "grants": {"read_my_email": {"granted_at": "x"}}}',
        encoding="utf-8")
    store = cc.CensusConsentStore(tmp_path)
    assert store.is_active("read_my_email") is False
    assert store.list_grants() == []


def test_a_broken_consent_file_grants_nothing(tmp_path, monkeypatch):
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    (tmp_path / "census_consent.json").write_text("{ not json", encoding="utf-8")
    assert ac.run_census(_vault(tmp_path))["facts_written"] == 0
    assert calls == [], "an unreadable consent file must fail CLOSED"


def test_a_grant_row_without_a_parseable_granted_at_is_not_active(tmp_path, monkeypatch):
    """UNMEASURED-CASE rejection (NOT integrity). A row with no parseable ``granted_at`` is
    not evidence of consent, so it must not read as the "scan now" signal — a bare
    ``{"grants": {"path_clis": {}}}`` used to authorise a scan. This also pins the LIMIT of
    the tightening: a hand-written row WITH a plausible timestamp still passes, because
    nothing here authenticates the writer (the integrity gap ``_load`` documents stays
    open — see ``test_no_production_grant_surface_exists`` and that docstring)."""
    calls: list = []
    _use_spy(monkeypatch, "path_clis", ["gh"], calls)
    store = cc.CensusConsentStore(tmp_path)
    v = _vault(tmp_path)

    # (1) a bare row — no granted_at — does NOT authorise a scan.
    (tmp_path / "census_consent.json").write_text(
        '{"version": 1, "grants": {"path_clis": {}}}', encoding="utf-8")
    assert store.is_active("path_clis") is False
    assert ac.run_census(v)["facts_written"] == 0
    assert calls == [], "a row with no granted_at must not reach the probe"

    # (2) an unparseable granted_at is rejected too.
    (tmp_path / "census_consent.json").write_text(
        '{"version": 1, "grants": {"path_clis": {"granted_at": "not-a-timestamp"}}}',
        encoding="utf-8")
    assert store.is_active("path_clis") is False

    # (3) a WELL-FORMED granted_at IS active — the point is malformed-input rejection, not
    # authentication, so a plausible (even hand-forged) timestamp passes.
    (tmp_path / "census_consent.json").write_text(
        '{"version": 1, "grants": {"path_clis": {"granted_at": "2026-07-21T10:00:00+00:00"}}}',
        encoding="utf-8")
    assert store.is_active("path_clis") is True
    assert ac.run_census(v, min_interval_seconds=0)["facts_written"] == 1


def test_an_unreadable_consent_file_shows_nothing_and_scans_nothing(tmp_path, monkeypatch):
    """``census_status`` returns ``[]`` ("watching nothing") on an unreadable store, and
    that reassuring value must never mask an active scan. It cannot, because ``run_census``
    gates on the SAME fail-closed ``_load``: an unreadable consent file makes BOTH the
    display show nothing AND the census scan nothing. Pins that coupling (see the
    ``census_status`` docstring), so the empty display can never be the healthy signal a
    failure path emits while the scanner runs."""
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    (tmp_path / "census_consent.json").write_text("{ not valid json", encoding="utf-8")
    v = _vault(tmp_path)
    assert ac.census_status(v) == [], "an unreadable store must display nothing"
    assert ac.run_census(v)["facts_written"] == 0, "and must scan nothing"
    assert calls == [], "no probe may be reached when the consent file is unreadable"


def test_the_grant_records_what_the_operator_agreed_to(tmp_path):
    v = _vault(tmp_path)
    card = ac.grant_category(v, "cloud_sync_roots")
    assert card["category"] == "cloud_sync_roots" and card["standing_scan"] is True
    rows = cc.CensusConsentStore(tmp_path).list_grants()
    assert [r["category"] for r in rows] == ["cloud_sync_roots"]
    assert rows[0]["granted_at"] and rows[0]["last_ran_at"] == "" and not rows[0]["paused"]


def test_last_ran_at_is_stamped_only_by_a_scan_that_actually_ran(tmp_path, monkeypatch):
    """``last_ran_at`` is the M3 "census last ran" surface, so it must be evidence of a
    SCAN, not of an attempt — otherwise a permanently-failing probe would report itself
    as running fine."""
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    consent = cc.CensusConsentStore(tmp_path)
    assert consent.last_ran_at("installed_apps") is None

    def _boom(limit, budget):
        raise RuntimeError("probe exploded")
    monkeypatch.setitem(ac.PROBES, "installed_apps", (_boom, "installed_application"))
    assert ac.run_census(v)["skipped"]["installed_apps"] == "probe_failed"
    assert consent.last_ran_at("installed_apps") is None, \
        "a failed probe must not stamp last_ran_at"

    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    ac.run_census(v, now="2026-07-21T10:00:00+00:00")
    assert consent.last_ran_at("installed_apps") == "2026-07-21T10:00:00+00:00"


# ══ budgets: an unbounded scan of a real machine is a cost AND a privacy problem ══

def test_a_category_is_capped_at_max_entries(tmp_path, monkeypatch):
    """The cap is passed to the probe AND enforced on what is stored, so a probe that
    ignores its limit still cannot flood the store."""
    seen_limits: list = []

    def _greedy(limit, budget):
        seen_limits.append(limit)
        return [f"App {i}" for i in range(limit + 50)]      # deliberately over-returns

    monkeypatch.setitem(ac.PROBES, "installed_apps", (_greedy, "installed_application"))
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    ac.run_census(v, max_entries=5)
    assert seen_limits == [5], "the cap must be handed to the probe"
    assert len(FactStore(v).all_facts()) == 5, \
        "a probe that ignores its limit must still be truncated"


def test_a_consented_category_does_not_rescan_within_the_min_interval(tmp_path, monkeypatch):
    calls: list = []
    _use_spy(monkeypatch, "installed_apps", ["Microsoft Excel"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    ac.run_census(v)
    again = ac.run_census(v)                                # default 6h interval
    assert len(calls) == 1
    assert again["skipped"]["installed_apps"] == "recently_scanned"
    ac.run_census(v, min_interval_seconds=0)
    assert len(calls) == 2, "once due, it scans again"


def test_the_default_rescan_interval_is_not_effectively_zero():
    """A cap that is really zero is indistinguishable from an absent one — the census
    would re-walk the registry on EVERY run."""
    assert ac.MIN_RESCAN_INTERVAL_SECONDS >= 3600
    assert ac.MAX_ENTRIES_PER_CATEGORY <= 1000
    assert 0 < ac.CENSUS_BUDGET_SECONDS <= 30


def test_an_unparseable_last_ran_stamp_fails_towards_scanning():
    """A corrupt timestamp must not permanently freeze a consented category; the cost of
    being wrong in this direction is one extra consented read-only scan."""
    assert ac._needs_rescan("not-a-timestamp", 10_000) is True
    assert ac._needs_rescan("", 10_000) is True
    assert ac._needs_rescan(None, 10_000) is True


def test_the_wall_clock_budget_stops_later_categories(tmp_path, monkeypatch):
    calls: list = []
    for cat in list(ac.PROBES):
        _use_spy(monkeypatch, cat, ["x"], calls)
    v = _vault(tmp_path)
    for cat in list(ac.PROBES):
        ac.grant_category(v, cat)
    summary = ac.run_census(v, budget_seconds=0.0)
    assert calls == [], "an exhausted budget must stop before the first probe"
    assert set(summary["skipped"].values()) == {"budget_exhausted"}


# ══ the probes themselves: real lookups, real bounds ═════════════════════════

def test_path_clis_reports_allowlisted_names_only_and_never_a_resolved_path(
        tmp_path, monkeypatch):
    """Drives the REAL ``shutil.which`` against a REAL directory on PATH.

    Two properties in one run: the allowlist is genuinely an allowlist (a non-listed
    executable sitting in the SAME directory is not reported), and the value is the NAME
    — never ``which``'s resolved path, which on Windows embeds the operator's username."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_fake_cli(bindir, "gh")                       # on KNOWN_CLIS
    _make_fake_cli(bindir, "acme-internal-deploy")     # NOT on KNOWN_CLIS
    monkeypatch.setenv("PATH", str(bindir))

    found = ac.probe_path_clis(50, ac._Budget(5.0))
    assert "gh" in found, "the real which() must find a real file on PATH"
    assert "acme-internal-deploy" not in found, "allowlist, not an enumeration of PATH"
    assert set(found) <= set(ac.KNOWN_CLIS)
    assert not any(("/" in f or "\\" in f) for f in found), "names, never paths"


def test_cloud_sync_roots_records_a_directory_only_when_it_exists(tmp_path, monkeypatch):
    """Existence-only: nothing inside is read or listed, and a stale environment
    variable pointing at a deleted folder yields nothing rather than a phantom fact."""
    _isolate_cloud_env(monkeypatch, tmp_path / "nohome")
    real = tmp_path / "OneDrive"
    real.mkdir()
    (real / "payslip.pdf").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("OneDrive", str(real))
    monkeypatch.setenv("OneDriveCommercial", str(tmp_path / "does-not-exist"))

    found = ac.probe_cloud_sync_roots(50, ac._Budget(5.0))
    assert found == [str(real)]
    assert not any("payslip" in f for f in found), "contents are never enumerated"


def test_a_whitespace_only_probe_value_never_becomes_a_fact():
    """`or`-style falsiness would let a whitespace-only registry DisplayName through —
    it is TRUTHY — and it would render as a blank row in the planner prompt."""
    facts = ac._facts_for("installed_apps", "installed_application",
                          ["   ", "", "\t\n", "Real App"], "2026-07-21T00:00:00+00:00")
    assert [f.value for f in facts] == ["Real App"]


def test_installed_apps_probe_runs_bounded_on_this_real_machine():
    """The probe is exercised against the REAL platform source (the Windows uninstall
    hive / /Applications / .desktop dirs), not a stub — a probe that only ever runs
    against a fixture is how a broken real path ships green. Asserts the BOUND, which is
    the property that must hold on any machine; an empty result is a legitimate answer
    (a bare container has no apps), so the count is not asserted."""
    values = ac.probe_installed_apps(7, ac._Budget(5.0))
    assert isinstance(values, list) and len(values) <= 7
    assert all(isinstance(v, str) and v.strip() for v in values)
    assert len(set(values)) == len(values), "deduped"


# ══ privacy: a credential value is never recorded ════════════════════════════

def test_the_census_never_reaches_a_credential_store():
    """Structural, not a filter. The census must not import or call anything that can
    return a secret VALUE — a heuristic scrubber on the way out would be a second path
    that can drift from the first."""
    import pathlib
    text = (pathlib.Path(ac.__file__)).read_text(encoding="utf-8", errors="replace")
    forbidden = ("credential_store", "CredentialStore", "get_secret", "list_secrets",
                 "keyring", "token_store", "TokenStore")
    hits = [f for f in forbidden if f in text]
    assert hits == [], f"the census must not reach credential material: {hits}"


def test_only_location_env_vars_are_read_and_only_as_paths(tmp_path, monkeypatch):
    """The cloud-sync probe is the one place the census reads an environment VALUE. Pin
    that it reads only the declared location variables — a probe that walked os.environ
    would sweep up tokens that live there."""
    _isolate_cloud_env(monkeypatch, tmp_path / "nohome")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", str(tmp_path))   # a real dir, so a
    monkeypatch.setenv("GITHUB_TOKEN", str(tmp_path))            # naive scan WOULD emit it
    assert ac.probe_cloud_sync_roots(50, ac._Budget(5.0)) == []
    assert "AWS_SECRET_ACCESS_KEY" not in ac._CLOUD_ENV_VARS


# ══ the M3 standing-scan operator surface ════════════════════════════════════

def test_the_world_cli_shows_what_the_census_is_still_watching(tmp_path, capsys):
    """WM-7/M3: a standing permission the operator cannot SEE is not meaningfully
    revocable. Asserted on an otherwise EMPTY store, because "granted but has found
    nothing yet" is the state the pre-existing empty-store early-return would hide."""
    from systemu.interface.cli_commands import run_world

    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")
    assert run_world(SimpleNamespace(root=tmp_path)) == 0
    out = capsys.readouterr().out
    assert "installed_apps" in out
    assert "never" in out, "an un-run category must say so, not render a blank"
    assert "standing" in out.lower(), "the ONGOING nature must be on the surface"
    assert "revok" in out.lower() and "delete" in out.lower()


def test_the_world_cli_says_nothing_about_the_census_when_nothing_is_granted(
        tmp_path, capsys):
    """Zero-census operation is INDISTINGUISHABLE, on the operator surface too."""
    from systemu.interface.cli_commands import run_world

    assert run_world(SimpleNamespace(root=tmp_path)) == 0
    out = capsys.readouterr().out
    assert "census" not in out.lower()
    assert "empty" in out.lower()


def test_a_paused_category_is_visibly_paused(tmp_path, capsys):
    from systemu.interface.cli_commands import run_world

    v = _vault(tmp_path)
    ac.grant_category(v, "path_clis")
    cc.CensusConsentStore(tmp_path).set_paused("path_clis", True)
    run_world(SimpleNamespace(root=tmp_path))
    assert "PAUSED" in capsys.readouterr().out


def test_census_status_is_read_only(tmp_path):
    """The display path must not create consent state — reading what is watched cannot
    be the thing that starts a watch."""
    v = _vault(tmp_path)
    assert ac.census_status(v) == []
    assert not (tmp_path / "census_consent.json").exists()


# ══ the call site — a census nothing calls is not a shipped feature ══════════

def test_the_census_is_wired_into_the_survey_seam():
    """Source-level, mirroring the existing populator seam pin in test_world_model.py.

    A correct producer that nothing invokes is indistinguishable from a shipped one, and
    the suite stays green either way — so the CALL is pinned, not just the function. This
    checks the call exists in the same post-survey block as the populator's."""
    import pathlib
    from systemu.runtime import world_model as wm
    text = (pathlib.Path(wm.__file__).parent / "shadow_runtime.py").read_text(
        encoding="utf-8", errors="replace")
    assert "from systemu.runtime.ambient_census import run_census" in text
    assert "run_census" in text.split("populate_from_situation")[-1], \
        "the census must be invoked at the post-survey seam"


# ══ TRUTHFULNESS PINS — the claims this feature makes about itself ═══════════
#
# R-W2 was held not because the code was wrong but because places in shipped source
# affirmatively claimed controls that do not exist. Prose cannot be trusted to stay true
# across a wiring commit, so the load-bearing claims are pinned here.

def _shipped_python_files():
    """Every .py file in the SHIPPED packages (not tests, not tools/scripts).

    Returned as (relative-path, text) so a failure can name the offender."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    out = []
    for pkg in ("systemu", "sharing_on", "extension", "plugins"):
        base = repo / pkg
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            try:
                out.append((p.relative_to(repo).as_posix(),
                            p.read_text(encoding="utf-8", errors="replace")))
            except Exception:
                continue
    return out


def test_the_shipped_file_scan_actually_finds_files():
    """Anti-vacuity for the scans below.

    A source scan that silently walks the wrong directory finds no violations and passes
    — indistinguishable from a clean tree. This pins that the scan sees a realistic
    package AND that it can see the very references the next test tolerates, so "found
    nothing" below means "nothing is there" rather than "looked nowhere"."""
    files = _shipped_python_files()
    assert len(files) > 200, f"shipped-file scan found only {len(files)} files"

    # PER-ROOT, not just the total: `len > 200` is satisfied by `systemu/` alone (~395
    # files), so a silently-broken rglob on `sharing_on/` or `plugins/` would still pass
    # while the grant-surface scan below quietly stopped covering those trees. Assert each
    # shipped package that HAS python is actually reached. (`extension/` ships no .py
    # today, so it is deliberately not asserted — a `> 0` there would be a false pin.)
    from collections import Counter
    per_root = Counter(rel.split("/", 1)[0] for rel, _ in files)
    assert per_root["systemu"] > 200, per_root
    assert per_root["sharing_on"] >= 10, per_root
    assert per_root["plugins"] >= 1, per_root

    joined = "\n".join(t for _, t in files)
    # The two KNOWN production references to the census. If the scan cannot see these,
    # it cannot see a grant surface either.
    assert "from systemu.runtime.ambient_census import census_status" in joined
    assert "from systemu.runtime.ambient_census import run_census" in joined


def test_no_production_grant_surface_exists():
    """NO SHIPPED FILE CAN CREATE A CENSUS GRANT — a SOURCE property, and all this pins.

    This is a grep over shipped source: no file outside the two census modules references
    the grant symbols, so no OPERATOR-reachable code path can call ``grant``. That is the
    real, checkable claim. It is NOT "the census is inert at runtime": ``run_census`` is
    wired into the survey seam and reads ``census_consent.json`` directly, so a consent
    file planted in the vault turns the census on regardless of what any source scan says.
    This test cannot see that file and does not try to — runtime inertness is a property
    of the disk, which no source scan can pin.

    What the shipped docstrings must therefore say (and, after the truthfulness pass, do):
    no operator grant SURFACE ships, so on a fresh install the census writes nothing — NOT
    that it can never run.

    WHEN THIS TEST FAILS, IT IS DOING ITS JOB. It means someone wired a grant surface, and
    the disclosures the failure message lists just became operator-visible. Do not delete
    it, and do not exempt the new file, without doing the work the failure message lists.

    Why a source scan rather than a behavioural one: the property is "no shipped code path
    can reach ``grant``", and a behavioural test can only demonstrate that the paths it
    happens to drive do not."""
    # EVERY needle is matched BARE, without a trailing paren — including the four function
    # names. A call-shaped needle (`grant_category(`) misses a re-export: a shipped file
    # doing `from systemu.runtime.ambient_census import grant_category` (that module is
    # excluded from the scan, so the definition does not save us) then calling it — under
    # its own name or an alias — is a real grant surface a `grant_category(`-only scan does
    # not see, because the re-export is from `ambient_census`, not from `census_consent`.
    # The import line always NAMES the symbol, so a bare match catches it. Verified that
    # these four names, `CensusConsentStore`, and `census_consent` are census-unique in the
    # shipped tree, so a bare match cannot cry wolf.
    #
    # A generic `.grant(` was deliberately NOT included. It matched
    # `Governor(config).grant(` in scheduler/jobs.py — an unrelated subsystem — and a
    # guard that cries wolf on the first unrelated file gets deleted rather than heeded.
    # `grant_category` and friends are specific enough to avoid that.
    needles = ("grant_category", "revoke_category", "consent_card", "set_paused",
               "CensusConsentStore", "census_consent")
    offenders = {}
    for rel, text in _shipped_python_files():
        # The two census modules DEFINE these; the guard is about EXTERNAL callers.
        if rel in ("systemu/runtime/ambient_census.py",
                   "systemu/runtime/census_consent.py"):
            continue
        for line in text.splitlines():
            s = line.lstrip()
            if s.startswith("#"):
                continue                    # a comment mention is not a call
            for needle in needles:
                if needle in line:
                    offenders.setdefault(rel, set()).add(needle)
    assert not offenders, (
        "\nA CENSUS CONSENT SURFACE APPEARED — the R-W2 disclosures are now stale.\n"
        f"  {({k: sorted(v) for k, v in offenders.items()})}\n"
        "\nBefore this lands, re-audit ALL of the following. They were written for a\n"
        "feature that could not run, and they are now operator-visible:\n"
        "  1. consent_card's `transmission_notice` / `leaves_this_machine`. Census facts\n"
        "     go into the planner prompt and are therefore sent to the model provider.\n"
        "     An earlier revision told the operator 'nothing is transmitted'. Confirm\n"
        "     the wording still matches the real render path before an operator reads it.\n"
        "  2. `revocation_surface_shipped: False` on the card, and the module docstring's\n"
        "     statement that no pause/revoke surface exists. If you shipped grant WITHOUT\n"
        "     revoke and pause, stop: a standing permission to enumerate the operator's\n"
        "     machine with no way to withdraw it is not a consent control.\n"
        "  3. CensusConsentStore._load has NO INTEGRITY CHECK. Consent is unsigned JSON,\n"
        "     so anything that can write one file into the vault can manufacture a grant\n"
        "     for every category. This is LIVE NOW, not latent: run_census has a production\n"
        "     caller (shadow_runtime) and reads this file directly, so a forged file scans\n"
        "     and transmits today. A grant surface only adds a legitimate 'yes' to forge on\n"
        "     top of that — it does not create the exposure.\n"
        "  4. The SCOPE section in ambient_census and the 'no grant surface' block in\n"
        "     census_consent both state this gap as current fact. Update them.\n"
        "  5. cli_commands.run_world tells the operator this build has no way to grant,\n"
        "     pause or revoke a category. That line becomes false.\n"
    )


def test_the_census_does_not_sweep_up_a_credential_env_value(tmp_path, monkeypatch):
    """The cloud-sync probe reads environment VALUES — the one place a probe could pick up
    a secret. The realistically-named credential decoys point at DISTINCT real directories
    (distinct on purpose: the probe dedupes by absolute path, so decoys sharing the
    allowlisted directory would collapse into its single entry and `count(...) == 1` would
    hold even for a probe that read every env var — the exact vacuity this test previously
    had). Only the allowlisted OneDrive path may come back; a probe that enumerated
    os.environ generically would also return the decoy dirs and fail this. Verified by
    mutating the probe's `_CLOUD_ENV_VARS` loop to `sorted(os.environ)`."""
    _isolate_cloud_env(monkeypatch, tmp_path / "nohome")     # clear the real machine's vars
    onedrive = tmp_path / "onedrive"; onedrive.mkdir()
    secret = tmp_path / "secret"; secret.mkdir()
    token = tmp_path / "token"; token.mkdir()
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", str(secret))  # a real dir, so a naive
    monkeypatch.setenv("GITHUB_TOKEN", str(token))            # env scan WOULD emit it
    monkeypatch.setenv("OneDrive", str(onedrive))            # the one allowlisted var
    values = ac.probe_cloud_sync_roots(50, ac._Budget(5.0))
    assert values == [str(onedrive)], (
        f"only the allowlisted OneDrive path may be recorded, never a credential var's "
        f"value; got {values}")


# ── the card must describe what the probe actually emits ────────────────────

def test_probe_output_matches_its_card_path_clis(tmp_path, monkeypatch):
    """`path_clis` claims to record NAMES and to exclude "the resolved file path".

    Driven against the REAL `shutil.which` probe, but on a PATH we CONTROL so the claim is
    checked on every machine: a real allowlisted CLI is planted, so the probe always emits
    at least one entry and the shape assertions actually run. (Previously this tolerated an
    empty result and so was silently vacuous on any CI image with none of KNOWN_CLIS on
    PATH — the M9 catch it advertises was machine-dependent.)"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_fake_cli(bindir, "git")                       # on KNOWN_CLIS
    monkeypatch.setenv("PATH", str(bindir))
    values = ac.probe_path_clis(50, ac._Budget(5.0))
    assert "git" in values, "the real which() must find the planted allowlisted CLI"
    for v in values:
        assert v in ac.KNOWN_CLIS, f"{v!r} is not on the published allowlist"
        assert "/" not in v and "\\" not in v and os.sep not in v, (
            f"{v!r} looks like a resolved path — the card excludes it because a resolved "
            f"path embeds the operator's username")


def test_installed_apps_card_does_not_claim_a_filter_the_probe_does_not_apply():
    """THE D4 REGRESSION PIN — the card said ``excludes: ["versions", "publishers"]``
    while the probe stored the vendor's DisplayName verbatim, versions and all. Observed
    real values at review time included patch-level strings for years-EOL runtimes.

    Two halves, in this order, because the second is only interesting given the first:

      1. demonstrate there IS no filter — a value carrying a version survives
         ``_facts_for`` byte-identically;
      2. therefore the card must not claim to exclude versions/publishers/editions.

    Asserting (2) alone would pin a string. Asserting (1) alone would pin a pass-through
    nobody promised anything about. The defect lives in the gap between them."""
    versioned = "SomeVendor Runtime 8 Update 241 (64-bit)"
    facts = ac._facts_for("installed_apps", "installed_application", [versioned], "T")
    assert len(facts) == 1
    assert facts[0].value == versioned, (
        "the probe now transforms DisplayName — if a version filter was added, the card "
        "may legitimately claim the exclusion again, but state exactly what it strips")

    card = cc.consent_card("installed_apps")
    excludes_blob = " ".join(card["excludes"]).lower()
    for claim in ("version", "publisher", "edition"):
        assert claim not in excludes_blob, (
            f"installed_apps claims to exclude {claim!r}, but the probe stores the "
            f"installer-authored DisplayName verbatim with no filter (demonstrated "
            f"above). Either strip it or do not claim it.")
    # The positive half: the operator is TOLD what the string really contains.
    collects_blob = " ".join(card["collects"]).lower()
    assert "version" in collects_blob, (
        "the card must state that the display name usually carries the version — that is "
        "the disclosure that replaced the false exclusion")


def test_the_consent_card_discloses_that_facts_leave_the_machine():
    """THE D3 REGRESSION PIN. ``stored_at`` used to read "this vault, on this machine —
    nothing is transmitted" while the census's entire designed payoff routes these facts
    into the planner prompt, which is an LLM call to the configured model provider.

    The disclosure is owed BECAUSE the render path exists, so the coupling is asserted
    here rather than a bare string match: the first half re-demonstrates that a stored
    census fact really does become prompt bytes."""
    # THE REASON THE DISCLOSURE IS OWED — re-demonstrated, not assumed.
    rendered = si.render_situation_for_prompt({
        "world_facts": [{"fact_id": "installed_application:x", "kind": "installed_application",
                         "value": "ZZSentinelApp 1.2.3", "bind_taint": "content_derived",
                         "staleness": "unknown"}]})
    assert "ZZSentinelApp 1.2.3" in rendered, (
        "a census-shaped world fact no longer reaches the planner prompt. If the "
        "census→prompt path was deliberately severed, the transmission disclosure below "
        "may be relaxed — but relax it knowingly.")

    for category in cc.CATEGORIES:
        card = cc.consent_card(category)
        assert card["leaves_this_machine"] is True, category
        assert "nothing is transmitted" not in card["stored_at"].lower(), (
            f"{category}: `stored_at` claims nothing is transmitted, but census facts "
            f"are rendered into the planner prompt and sent to the model provider.")
        assert "model provider" in card["transmission_notice"].lower(), category
        # The card must not promise a control this build does not have.
        assert card["revocation_surface_shipped"] is False, (
            f"{category}: the card says a revocation surface ships. If that is now true, "
            f"test_no_production_grant_surface_exists should already have failed — and "
            f"every disclosure it names needs re-reading.")


def test_the_standing_scan_block_survives_the_query_early_return(tmp_path, capsys):
    """D12 — `sharing_on world <query>` used to hide the whole census disclosure.

    The block was rendered AFTER the `if q:` branch's `return 0`, so the query form —
    the one an operator reaches for most — silently never showed what was being watched.
    A standing permission you can only see by running the bare command is not one the
    operator can be said to be aware of.

    Driven through BOTH forms of the real CLI, and the bare form is asserted in the same
    test on purpose: a fix that moved the block and broke the original surface would
    otherwise read as a pass."""
    from systemu.interface.cli_commands import run_world

    v = _vault(tmp_path)
    ac.grant_category(v, "installed_apps")

    assert run_world(SimpleNamespace(root=tmp_path), query="excel") == 0
    queried = capsys.readouterr().out
    assert "installed_apps" in queried, (
        "the standing-scan disclosure is hidden behind the query early-return again")
    assert "standing" in queried.lower()

    assert run_world(SimpleNamespace(root=tmp_path)) == 0
    bare = capsys.readouterr().out
    assert "installed_apps" in bare, "the bare form lost the disclosure"
