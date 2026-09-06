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


# ══ the consent file is AUTHENTICATED — a planted file cannot manufacture a "yes" ══
#
# Re-audit item 3 of the (now replaced) grant-surface pin. Before the census consent
# surface shipped, `_load` trusted unsigned JSON: anything that could place one file in
# the vault manufactured a grant for EVERY category, and `run_census` already had a live
# production caller, so the forged grant scanned the machine and routed the results into
# the planner prompt on the next survey. The grant surface did not create that exposure;
# it added a legitimate "yes" on top of it. The authenticator closes it.
#
# EVERY test below carries its POSITIVE CONTROL in the same test. "is_active is False"
# is also what a completely broken consent store produces, so an assertion that only
# shows the rejected case cannot distinguish "the MAC refused this" from "nothing works"
# (DEC-32: a pin asserting a value the failure path also produces is not a pin).


def _write_raw_consent(base, obj) -> None:
    """Place a hand-built consent file — the forger's capability, exactly."""
    import json as _json
    Path(base).mkdir(parents=True, exist_ok=True)
    (Path(base) / "census_consent.json").write_text(
        _json.dumps(obj, indent=2), encoding="utf-8")


def _read_raw_consent(base):
    import json as _json
    return _json.loads((Path(base) / "census_consent.json").read_text(encoding="utf-8"))


def _write_signed_consent(base, grants) -> None:
    """A consent file with a VALID MAC for ``base``'s own key, carrying exactly ``grants``.

    Signs with the production helpers rather than reimplementing them, so a test that
    wants to probe a layer ABOVE the authenticator (row shape, category vocabulary) can
    hand it a genuinely-signed file instead of accidentally measuring the MAC."""
    ordered = {k: grants[k] for k in sorted(grants)}
    key = cc._consent_key(Path(base))
    _write_raw_consent(base, {
        "version": cc.CONSENT_FORMAT_VERSION, "grants": ordered,
        "mac": cc._consent_mac(key, cc.CONSENT_FORMAT_VERSION, ordered)})


def test_an_unsigned_consent_file_grants_nothing_and_scans_nothing(tmp_path, monkeypatch):
    """THE FORGED-GRANT PIN. A well-formed, plausible, UNSIGNED consent file — the exact
    artifact a hand edit, a restored backup or a file-write-capable tool could drop in —
    must authorise nothing, and must not reach a probe.

    The positive control runs the SAME probe object against the SAME vault and changes
    only HOW the grant got there (real API vs. planted file), so the empty `calls` in the
    first phase cannot be explained by a broken probe, store or fact builder."""
    calls: list = []
    _use_spy(monkeypatch, "cloud_sync_roots", ["C:/Users/x/OneDrive"], calls)
    v = _vault(tmp_path)

    _write_raw_consent(tmp_path, {"version": 1, "grants": {
        "cloud_sync_roots": {"granted_at": "2026-01-01T00:00:00+00:00"},
        "installed_apps": {"granted_at": "2026-01-01T00:00:00+00:00"},
        "path_clis": {"granted_at": "2026-01-01T00:00:00+00:00"}}})
    store = cc.CensusConsentStore(tmp_path)
    assert store.list_grants() == [], "an unsigned file must read as NO grants at all"
    for cat in cc.CATEGORIES:
        assert store.is_active(cat) is False, cat
    forged = ac.run_census(v)
    assert calls == [], "a planted consent file must never reach a probe"
    assert forged["facts_written"] == 0
    assert set(forged["skipped"].values()) == {"not_consented"}

    # POSITIVE CONTROL — the only change is that consent is created through the API.
    ac.grant_category(v, "cloud_sync_roots")
    granted = ac.run_census(v)
    assert calls, "positive control: a genuine grant must reach the same probe"
    assert granted["scanned"] == ["cloud_sync_roots"]


def test_the_pre_authenticator_format_is_never_grandfathered(tmp_path):
    """MIGRATION RULE, pinned. The unsigned v1 file is the format that shipped, so a real
    vault can hold one. It reads as UNCONSENTED — it is not upgraded in place, not
    trusted "because it was already there", and not re-signed on read. Re-granting is the
    only way back, which is correct: the operator's answer is what a grant records, and
    nothing on disk can prove the v1 file ever carried one."""
    real = cc.CensusConsentStore(tmp_path)
    real.grant("cloud_sync_roots")
    signed = _read_raw_consent(tmp_path)
    assert signed["version"] == cc.CONSENT_FORMAT_VERSION >= 2
    assert real.is_active("cloud_sync_roots") is True          # control: this works

    # Downgrade it to the exact pre-authenticator shape, keeping the same grant row.
    _write_raw_consent(tmp_path, {"version": 1, "grants": signed["grants"]})
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is False
    # ...and reading it did NOT quietly rewrite/upgrade the file.
    assert _read_raw_consent(tmp_path)["version"] == 1


def test_tampering_with_a_signed_file_invalidates_every_grant(tmp_path):
    """The MAC covers the whole grants object, so an attacker cannot ADD a category to a
    file the operator legitimately signed, nor un-pause one, nor backdate one. The
    failure is whole-file (all grants drop), which is the fail-closed direction: a
    partially-trusted consent file is not a thing this store will produce."""
    store = cc.CensusConsentStore(tmp_path)
    store.grant("cloud_sync_roots")
    assert store.is_active("cloud_sync_roots") is True          # control

    signed = _read_raw_consent(tmp_path)
    # The realistic attack: keep the operator's real grant + its real MAC, bolt on the
    # category they never consented to.
    signed["grants"]["installed_apps"] = dict(signed["grants"]["cloud_sync_roots"])
    _write_raw_consent(tmp_path, signed)
    after = cc.CensusConsentStore(tmp_path)
    assert after.is_active("installed_apps") is False, "an added category must not stick"
    assert after.is_active("cloud_sync_roots") is False, (
        "a tampered file must not keep authorising the untouched rows either — the MAC "
        "covers the whole grants object, so 'partly valid' is not a state")


def test_an_unkeyed_digest_is_not_accepted_as_the_mac(tmp_path):
    """The authenticator must be KEYED. An unkeyed hash reads as integrity while
    providing none: the forger computes it as easily as the store does, so a file signed
    with `sha256(canonical_body)` would be a complete bypass. This drives the actual
    forgery."""
    import hashlib as _h
    store = cc.CensusConsentStore(tmp_path)
    store.grant("cloud_sync_roots")
    assert store.is_active("cloud_sync_roots") is True          # control

    grants = {"installed_apps": {"granted_at": "2026-01-01T00:00:00+00:00",
                                 "paused": False}}
    body = cc._canonical_body(cc.CONSENT_FORMAT_VERSION, grants)
    for forged_mac in (_h.sha256(body).hexdigest(),
                       _h.sha256(cc._CONSENT_MAC_PREFIX + body).hexdigest()):
        _write_raw_consent(tmp_path, {"version": cc.CONSENT_FORMAT_VERSION,
                                      "grants": grants, "mac": forged_mac})
        assert cc.CensusConsentStore(tmp_path).is_active("installed_apps") is False, (
            "an unkeyed digest over the canonical body was accepted as the MAC — the "
            "authenticator is not keyed and every grant is forgeable")


def test_a_consent_file_signed_for_another_vault_is_rejected(tmp_path):
    """The key is PER-VAULT (derived from that vault's own persisted secret), so a
    consent file lifted from one vault does not authorise a scan in another. Copying a
    genuinely-signed file is the forgery a global key would not stop."""
    a, b = tmp_path / "vault_a", tmp_path / "vault_b"
    cc.CensusConsentStore(a).grant("cloud_sync_roots")
    assert cc.CensusConsentStore(a).is_active("cloud_sync_roots") is True   # control
    cc.CensusConsentStore(b).grant("path_clis")                # give B its own key+file
    assert cc.CensusConsentStore(b).is_active("path_clis") is True          # control

    _write_raw_consent(b, _read_raw_consent(a))                # lift A's signed file
    moved = cc.CensusConsentStore(b)
    assert moved.is_active("cloud_sync_roots") is False, (
        "a consent file signed by another vault's key was honoured — the MAC key is not "
        "per-vault, so one leaked/known key forges consent everywhere")
    assert moved.list_grants() == []


def test_no_vault_key_means_no_grants(tmp_path, monkeypatch):
    """FAIL-CLOSED on the key itself. If the per-vault secret cannot be obtained, there
    is no way to tell a genuine consent file from a planted one — so nothing is
    consented and nothing scans. (The alternative, trusting the file when the key is
    missing, is a bypass anyone can trigger by deleting one file.)"""
    from systemu.runtime import dashboard_auth
    calls: list = []
    _use_spy(monkeypatch, "cloud_sync_roots", ["C:/Users/x/OneDrive"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    assert ac.run_census(v)["scanned"] == ["cloud_sync_roots"]   # control
    assert len(calls) == 1

    monkeypatch.setattr(dashboard_auth, "session_secret", lambda _v: "")
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is False
    assert cc.CensusConsentStore(tmp_path).list_grants() == []
    blind = ac.run_census(v, min_interval_seconds=0)
    assert len(calls) == 1, "with no key, a previously-granted category must not scan"
    assert blind["skipped"]["cloud_sync_roots"] == "not_consented"


def test_the_mac_comparison_is_constant_time(tmp_path, monkeypatch):
    """DEC-34: never compare an authenticator with a polymorphic operator against an
    attacker-controlled operand. Asserted on the CALL, not on the source text — a source
    grep passes on a `compare_digest` that is imported and never used."""
    store = cc.CensusConsentStore(tmp_path)
    store.grant("cloud_sync_roots")
    seen: list = []
    real = cc.hmac.compare_digest

    def _spy(a, b):
        seen.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(cc.hmac, "compare_digest", _spy)
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is True
    assert seen, "the consent MAC was verified without hmac.compare_digest"
    assert all(t is bytes for pair in seen for t in pair), (
        f"compare_digest must be handed BYTES on both sides (it raises on a non-ASCII "
        f"str, and a file-controlled operand can carry one); got {seen}")


def test_a_fresh_install_read_does_not_mint_a_vault_secret(tmp_path):
    """`run_census` runs on EVERY survey, and on a fresh install there is no consent
    file. The absent-file read must short-circuit before the key is derived: deriving it
    would make a read-only privacy check get-or-CREATE a persisted vault secret on every
    default install, which is a side effect the census has no business having."""
    v = _vault(tmp_path)
    assert ac.run_census(v)["facts_written"] == 0
    assert ac.census_status(v) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [], (
        "reading an absent consent file touched the vault — nothing at all should be "
        "created by a census run on a fresh install")


# ══ AC5 clause 3 — a census capability wins a plan, unnamed by the operator ══

@pytest.mark.asyncio
# DEC-44: the `real_survey` marker was dropped here - the conftest survey stub is
# opt-in now, so the REAL survey this test needs is simply the default.
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
# DEC-44: the `real_survey` marker was dropped here - the conftest survey stub is
# opt-in now, so the REAL survey this test needs is simply the default.
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
    """UNMEASURED-CASE rejection, a layer ABOVE the MAC. A row with no parseable
    ``granted_at`` is not evidence of consent, so it must not read as the "scan now"
    signal — a bare ``{"grants": {"path_clis": {}}}`` used to authorise a scan.

    Every phase writes a CORRECTLY SIGNED file, so what is measured here is
    :meth:`is_active`'s shape check and not the authenticator: a malformed row must be
    refused even when it carries a valid MAC (which it can, since systemu's own writer
    signs whatever it is handed). Phase (4) then pins that the two layers are
    independent — a well-formed row with NO valid signature is still inactive, so the
    tightening did not quietly get absorbed into the MAC check."""
    calls: list = []
    _use_spy(monkeypatch, "path_clis", ["gh"], calls)
    store = cc.CensusConsentStore(tmp_path)
    v = _vault(tmp_path)

    # (1) a bare row — no granted_at — does NOT authorise a scan.
    _write_signed_consent(tmp_path, {"path_clis": {}})
    assert store.is_active("path_clis") is False
    assert ac.run_census(v)["facts_written"] == 0
    assert calls == [], "a row with no granted_at must not reach the probe"

    # (2) an unparseable granted_at is rejected too.
    _write_signed_consent(tmp_path, {"path_clis": {"granted_at": "not-a-timestamp"}})
    assert store.is_active("path_clis") is False

    # (3) a WELL-FORMED, SIGNED granted_at IS active — the positive control that makes
    # (1) and (2) mean "the shape was refused" rather than "nothing works here".
    _write_signed_consent(
        tmp_path, {"path_clis": {"granted_at": "2026-07-21T10:00:00+00:00"}})
    assert store.is_active("path_clis") is True
    assert ac.run_census(v, min_interval_seconds=0)["facts_written"] == 1

    # (4) the SAME well-formed row, unsigned, is inactive: two independent gates.
    (tmp_path / "census_consent.json").write_text(
        '{"version": 1, "grants": {"path_clis": {"granted_at": "2026-07-21T10:00:00+00:00"}}}',
        encoding="utf-8")
    assert store.is_active("path_clis") is False, (
        "a well-formed but UNSIGNED grant row authorised a scan — the authenticator is "
        "not gating, only the shape check is")


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


#: The marker each surface file carries immediately before its census code. The scan
#: below anchors on it, so "the census consent surface lives in ONE declared region of
#: each file" is a checkable property rather than a convention.
_CENSUS_REGION_MARKER = "R-W2 CENSUS CONSENT SURFACE :: REGION START"

#: The ONLY shipped files that may reference the census grant/consent symbols, and the
#: role each plays. Anything else is a widening.
_CENSUS_SURFACE_FILES = {
    "systemu/interface/cli_commands.py": "the run_census_* command implementations",
    "sharing_on/cli.py": "the `systemu census` click group that calls them",
}

#: EVERY needle is matched BARE, without a trailing paren — including the function names.
#: A call-shaped needle (`grant_category(`) misses a re-export: a shipped file doing
#: `from systemu.runtime.ambient_census import grant_category` (that module is excluded
#: from the scan, so the definition does not save us) then calling it — under its own name
#: or an alias — is a real grant surface a `grant_category(`-only scan does not see. The
#: import line always NAMES the symbol, so a bare match catches it. Verified that these
#: names are census-unique in the shipped tree, so a bare match cannot cry wolf.
#:
#: The `run_census_*` names are needles TOO, and that is not belt-and-braces: they are
#: themselves grant-creating entry points now, so a dashboard route doing
#: `from systemu.interface.cli_commands import run_census_grant` would create operator
#: consent from a surface nobody reviewed while every underlying symbol stayed confined.
#: Scanning only the library layer would have missed exactly that.
#:
#: NOT included: `census_status` / `run_census_status` (read-only display — a guard that
#: cries wolf on a legitimate display surface gets deleted rather than heeded) and a
#: generic `.grant(`, which matched `Governor(config).grant(` in scheduler/jobs.py.
_CENSUS_GRANT_NEEDLES = ("grant_category", "revoke_category", "pause_category",
                         "consent_card", "set_paused",
                         "CensusConsentStore", "census_consent",
                         "run_census_grant", "run_census_revoke",
                         "run_census_pause", "run_census_resume")


def test_the_census_grant_surface_is_confined_to_its_declared_region():
    """THE NARROWED GRANT-SURFACE SCAN — successor to
    ``test_no_production_grant_surface_exists``, which asserted that NO shipped file could
    create a census grant. That is no longer true and must not be: P4-B2 shipped the
    operator surface for ``cloud_sync_roots``, so §5.11 AC5 clause 3 is finally reachable
    on a default install instead of by test only. The predecessor's own failure message
    named this narrowing as the procedure, and its five re-audits were carried out in the
    same change (see the module docstrings, the card, and ``run_world``).

    WHAT THIS STILL PINS, and why it is worth as much as its predecessor: the surface is
    confined to TWO files and, within each, to ONE declared region. So the property
    "there is exactly one place an operator's census consent can be created, and you can
    read all of it at once" survives. A grant path sprouting in a dashboard route, a
    registered tool, an elicitation handler, or halfway up ``cli_commands.py`` fails here.

    It remains a SOURCE property. It cannot pin that no consent file exists on a given
    disk — nothing can — but that matters far less now: since the authenticator landed,
    a file planted in the vault does not grant anything (see
    ``test_an_unsigned_consent_file_grants_nothing_and_scans_nothing``).

    WHEN THIS TEST FAILS, IT IS DOING ITS JOB. Read the failure message before exempting
    anything."""
    needles = _CENSUS_GRANT_NEEDLES
    outside_files = {}          # a file that may not reference the census at all
    outside_region = {}         # an allowed file, referencing it OUTSIDE its region
    for rel, text in _shipped_python_files():
        # The two census modules DEFINE these; the guard is about EXTERNAL callers.
        if rel in ("systemu/runtime/ambient_census.py",
                   "systemu/runtime/census_consent.py"):
            continue
        lines = text.splitlines()
        allowed = rel in _CENSUS_SURFACE_FILES
        # A missing marker makes `region_at` len(lines), so EVERY reference reads as
        # out-of-region and the test fails loudly. Silently treating "no marker" as
        # "no constraint" is how a region guard becomes decorative.
        region_at = next((i for i, ln in enumerate(lines)
                          if _CENSUS_REGION_MARKER in ln), len(lines))
        for i, line in enumerate(lines):
            s = line.lstrip()
            if s.startswith("#") and _CENSUS_REGION_MARKER not in line:
                continue                    # a comment mention is not a call
            for needle in needles:
                if needle not in line:
                    continue
                if not allowed:
                    outside_files.setdefault(rel, set()).add(needle)
                elif i < region_at:
                    outside_region.setdefault(rel, set()).add(f"L{i + 1}:{needle}")
    assert not outside_files and not outside_region, (
        "\nTHE CENSUS CONSENT SURFACE ESCAPED ITS DECLARED REGION.\n"
        f"  files that may not touch it at all: "
        f"{({k: sorted(v) for k, v in outside_files.items()})}\n"
        f"  allowed files, but ABOVE the region marker: "
        f"{({k: sorted(v) for k, v in outside_region.items()})}\n"
        f"\nThe surface is confined to, and only to:\n"
        + "".join(f"  - {f}  ({why})\n" for f, why in sorted(_CENSUS_SURFACE_FILES.items()))
        + f"  ...each below a line containing {_CENSUS_REGION_MARKER!r}.\n"
        "\nIf you are WIDENING it (a dashboard control, a registered tool, an\n"
        "elicitation surface, a second category), re-audit ALL of the following first —\n"
        "these are the five the predecessor pin named, kept because a widening makes each\n"
        "operator-visible again on a path nobody has read:\n"
        "  1. consent_card's `transmission_notice` / `leaves_this_machine`. Census facts\n"
        "     go into the planner prompt and are therefore sent to the model provider.\n"
        "     An earlier revision told the operator 'nothing is transmitted'. Confirm\n"
        "     the wording still matches the real render path on YOUR new surface — a\n"
        "     renderer that shows only `collects` drops the whole disclosure.\n"
        "  2. `revocation_surface_shipped` is per-category and true ONLY for the\n"
        "     categories in census_consent.SURFACED_CATEGORIES. If you shipped grant for\n"
        "     a new category WITHOUT revoke and pause, stop: a standing permission to\n"
        "     enumerate the operator's machine with no way to withdraw it is not a\n"
        "     consent control.\n"
        "  3. The consent file's integrity. It is authenticated now (HMAC over the\n"
        "     canonical body, per-vault key, unsigned v1 never grandfathered) -- do not\n"
        "     add a second writer that bypasses CensusConsentStore._write, and do not\n"
        "     add a read path that accepts an unverified file.\n"
        "  4. The SCOPE section in ambient_census and the surface block in\n"
        "     census_consent state exactly which categories an operator can reach.\n"
        "     A new surface makes both stale.\n"
        "  5. cli_commands.run_world tells the operator which controls exist. It names\n"
        "     the CLI commands today; a second surface makes that half the story.\n"
    )


def test_the_region_guard_is_not_vacuous():
    """The scan above is only meaningful if the marker it anchors on really is in both
    files and really does sit ABOVE the census code. Pinned directly, because a typo'd
    marker would make ``region_at`` fall back to end-of-file and turn every reference into
    a failure — loud — while a marker accidentally placed at line 1 would make the region
    check pass for the whole file, which is SILENT."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    for rel in _CENSUS_SURFACE_FILES:
        lines = (repo / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        marks = [i for i, ln in enumerate(lines) if _CENSUS_REGION_MARKER in ln]
        assert len(marks) == 1, f"{rel}: expected exactly 1 region marker, got {len(marks)}"
        assert marks[0] > 0.5 * len(lines), (
            f"{rel}: the census region marker is at line {marks[0] + 1} of {len(lines)} — "
            f"a marker near the top of the file makes the region check cover everything "
            f"and pin nothing. The census surface belongs in its own block at the end.")
        # Comment lines are skipped here for the SAME reason the scan skips them: a
        # comment mention is not a call. Without this, a stray "census_consent.json"
        # in an unrelated docstring block would satisfy the vacuity check while the
        # region held no actual surface.
        hits = [i for i, ln in enumerate(lines)
                if not ln.lstrip().startswith("#")
                and any(n in ln for n in _CENSUS_GRANT_NEEDLES)]
        assert hits and min(hits) > marks[0], (
            f"{rel}: the region check would pass vacuously — no grant reference below the "
            f"marker means the scan is not measuring this file's surface at all")


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
        # The card must not promise a control this build does not have, NOR deny one it
        # does. Per-category since P4-B2: `cloud_sync_roots` ships grant/revoke/pause, the
        # other two do not. A blanket True would be the overclaim R-W2 was held for; a
        # blanket False would make the shipped card contradict the CLI the operator just
        # used. The flag must track census_consent.SURFACED_CATEGORIES exactly.
        assert card["revocation_surface_shipped"] is (
            category in cc.SURFACED_CATEGORIES), (
            f"{category}: `revocation_surface_shipped` disagrees with "
            f"SURFACED_CATEGORIES={cc.SURFACED_CATEGORIES!r}. If you shipped or withdrew "
            f"an operator surface, every disclosure named in "
            f"test_the_census_grant_surface_is_confined_to_its_declared_region needs "
            f"re-reading.")


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


# ══ P4-B2 — THE OPERATOR SURFACE (`systemu census ...`) ══════════════════════
#
# One category ships end to end: `cloud_sync_roots`, the lowest-privacy of the three.
# All three verbs land together, because a standing permission to enumerate the
# operator's machine with no way to withdraw it is not a consent control (the predecessor
# pin's re-audit item 2, which forbade shipping grant alone).


def _cloud_machine(tmp_path, monkeypatch):
    """A machine shape with ONE detectable cloud-sync root, faked at the probe's
    FILESYSTEM/ENV inputs rather than by stubbing the probe.

    That distinction is the whole point of the AC5 pin below: a stubbed probe would
    demonstrate the plumbing while leaving "does the real, shipped probe run when the
    operator consents through the real, shipped CLI" untested — which is exactly the
    half-built shape this slice exists to close. Returns the root the probe must find."""
    home = tmp_path / "home"
    home.mkdir()
    _isolate_cloud_env(monkeypatch, home)          # clear the developer's real machine
    root = tmp_path / "cloud" / "OneDrive"
    root.mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(root))
    return root


def test_the_default_install_path_grants_scans_and_revokes_end_to_end(tmp_path, monkeypatch):
    """§5.11 AC5 CLAUSE 3, DEMONSTRATED ON THE DEFAULT INSTALL PATH FOR THE FIRST TIME.

    Until this slice, every AC5 demonstration reached ``grant_category`` from a test. No
    operator could, so the whole payoff — "a census-discovered capability is available to
    a plan without the operator naming it" — was a property of the test suite rather than
    of the product. This drives the REAL CLI entry points against the REAL probe:

        real `census grant`  ->  run_census (the same call shadow_runtime makes)
        ->  a fact in the store  ->  the fact in `world_facts`  ->  the planner prompt
        ->  real `census revoke`  ->  the next run SKIPS the category
        ->  and the fact is GONE from the store, the view, and the prompt.

    NOTHING about the consent is faked: the grant is created by the shipped command and
    lands in a signed consent file. Only the machine's cloud-sync shape is arranged."""
    from systemu.interface.cli_commands import run_census_grant, run_census_revoke

    root = _cloud_machine(tmp_path, monkeypatch)
    vault_dir = tmp_path / "vault"
    v = _vault(vault_dir)

    # --- precondition: unconsented, the real probe is never reached ---------------
    assert ac.run_census(v)["skipped"]["cloud_sync_roots"] == "not_consented"
    assert FactStore(v).all_facts() == []

    # --- grant, through the shipped command ---------------------------------------
    assert run_census_grant(v, "cloud_sync_roots", assume_yes=True) == 0
    assert cc.CensusConsentStore(vault_dir).is_active("cloud_sync_roots") is True

    summary = ac.run_census(v)
    assert summary["scanned"] == ["cloud_sync_roots"], summary
    assert [(f.kind, f.value) for f in FactStore(v).all_facts()] == \
        [("cloud_sync_root", str(root))], "the REAL probe must find the arranged root"

    # --- the payoff: it reaches world_facts, and from there the planner prompt -----
    view = si.compose_world_view(si.SituationReport(), v,
                                 "put my quarterly notes somewhere synced")
    assert [(r["kind"], r["value"]) for r in view.world_facts] == \
        [("cloud_sync_root", str(root))]
    # The renderer emits JSON, so a Windows path arrives with its separators escaped.
    # Compare against the JSON-escaped form rather than loosening the assertion to a
    # substring like "OneDrive" — the value that must cross the boundary is the whole
    # path, and that is what the transmission disclosure is about.
    import json as _json
    on_the_wire = _json.dumps(str(root))[1:-1]
    rendered = si.render_situation_for_prompt(view.model_dump())
    assert on_the_wire in rendered, "a consented census fact must reach the planner prompt"
    assert "untrusted_inventory_data" in rendered, "...and arrive FENCED (WM-15)"

    # --- revoke, through the shipped command ---------------------------------------
    assert run_census_revoke(v, "cloud_sync_roots") == 0
    assert cc.CensusConsentStore(vault_dir).is_granted("cloud_sync_roots") is False

    after = ac.run_census(v, min_interval_seconds=0)
    assert after["skipped"]["cloud_sync_roots"] == "not_consented", \
        "revocation must stop FUTURE scans, not just this one"
    assert after["facts_written"] == 0

    # The spec asks that revocation reach the DERIVED FACTS. This codebase implements the
    # strongest form of that: `revoke_category` PURGES them via the provenance ref
    # (`FactStore.purge_source_ref`), so there is no stale row left to read. Asserted at
    # all three surfaces the fact previously reached, because "gone from the store" and
    # "gone from what the model is told" are different claims.
    assert FactStore(v).all_facts() == []
    revoked_view = si.compose_world_view(si.SituationReport(), v,
                                         "put my quarterly notes somewhere synced")
    assert revoked_view.world_facts == []
    assert on_the_wire not in si.render_situation_for_prompt(revoked_view.model_dump())


def test_the_grant_command_shows_the_real_card_including_the_transmission_notice(
        tmp_path, monkeypatch, capsys):
    """RE-AUDIT ITEM 1, enforced on the surface that renders it. The card is the operator's
    only chance to learn that consenting sends these values to the model provider, and a
    renderer that prints only ``collects``/``excludes`` silently drops that.

    Asserted against ``consent_card``'s OWN text rather than a hand-copied string, so the
    disclosure and the thing that renders it cannot drift apart."""
    from systemu.interface.cli_commands import run_census_grant

    _cloud_machine(tmp_path, monkeypatch)
    card = cc.consent_card("cloud_sync_roots")
    assert run_census_grant(_vault(tmp_path / "v"), "cloud_sync_roots",
                            assume_yes=True) == 0
    out = capsys.readouterr().out

    for field in ("transmission_notice", "standing_scan_notice", "revocation_notice",
                  "why", "how", "stored_at", "title"):
        assert card[field] in out, f"the grant surface did not render the card's {field}"
    for item in card["collects"] + card["excludes"]:
        assert item in out, f"the grant surface dropped a disclosure item: {item!r}"
    # The two claims most easily lost in a summary render.
    assert "model provider" in out.lower()
    assert "standing" in out.lower()


def test_grant_defaults_to_no_and_records_nothing_when_declined(tmp_path, monkeypatch):
    """Typed y/N with default N. A consent prompt whose default is "yes" is not consent,
    and a decline must leave NOTHING behind — no consent file, no scan."""
    from systemu.interface import cli_commands as cli

    _cloud_machine(tmp_path, monkeypatch)
    vault_dir = tmp_path / "v"
    v = _vault(vault_dir)
    seen: list = []

    def _fake_confirm(text, **kw):
        seen.append(kw.get("default"))
        return False

    monkeypatch.setattr(cli, "_census_stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(cli.click, "confirm", _fake_confirm)
    assert cli.run_census_grant(v, "cloud_sync_roots") != 0
    assert seen == [False], "the confirm must default to N"
    assert not (vault_dir / "census_consent.json").exists()
    assert ac.run_census(v)["facts_written"] == 0

    # POSITIVE CONTROL — the same call, answered yes, does grant. Without this, the
    # assertions above are satisfied by a command that never works at all.
    monkeypatch.setattr(cli.click, "confirm", lambda text, **kw: True)
    assert cli.run_census_grant(v, "cloud_sync_roots") == 0
    assert cc.CensusConsentStore(vault_dir).is_active("cloud_sync_roots") is True


def test_grant_refuses_without_a_terminal_and_names_the_flag(tmp_path, monkeypatch, capsys):
    """Non-TTY (Docker / CI / a service) must REFUSE rather than prompt into a closed
    stdin — and must name ``--yes``, because a refusal with no way forward is the F3 shape
    this codebase has already been bitten by. ``--yes`` is the operator asserting they read
    the disclosure; it is not a way to skip printing it."""
    from systemu.interface import cli_commands as cli

    _cloud_machine(tmp_path, monkeypatch)
    vault_dir = tmp_path / "v"
    monkeypatch.setattr(cli, "_census_stdin_is_a_terminal", lambda: False)

    def _must_not_prompt(*a, **kw):                     # pragma: no cover - the assertion
        raise AssertionError("prompted with no terminal instead of refusing")

    monkeypatch.setattr(cli.click, "confirm", _must_not_prompt)
    assert cli.run_census_grant(_vault(vault_dir), "cloud_sync_roots") != 0
    out = capsys.readouterr().out
    assert "--yes" in out, "the refusal must name the flag that unblocks it"
    assert not (vault_dir / "census_consent.json").exists()
    # ...and the disclosure was still shown, so the operator can read what --yes means.
    assert cc.consent_card("cloud_sync_roots")["transmission_notice"] in out


def test_only_the_shipped_category_is_grantable(tmp_path, monkeypatch, capsys):
    """This slice ships ONE category end to end. The other two must be refused HONESTLY —
    named as not-yet-grantable-from-this-build — rather than silently accepted, silently
    ignored, or presented as if they were on offer."""
    from systemu.interface import cli_commands as cli

    _cloud_machine(tmp_path, monkeypatch)
    vault_dir = tmp_path / "v"
    v = _vault(vault_dir)
    monkeypatch.setattr(cli, "_census_stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(cli.click, "confirm", lambda text, **kw: True)

    assert set(cc.SURFACED_CATEGORIES) == {"cloud_sync_roots"}
    for category in set(cc.CATEGORIES) - set(cc.SURFACED_CATEGORIES):
        assert cli.run_census_grant(v, category) != 0, category
        out = capsys.readouterr().out.lower()
        assert "not yet grantable" in out, category
        assert cc.CensusConsentStore(vault_dir).is_granted(category) is False

    # An unknown category is refused too — loudly, never as a silent no-op that would
    # read to the operator as a granted capability that is quietly dead.
    assert cli.run_census_grant(v, "printers") != 0
    assert "printers" in capsys.readouterr().out


def test_pause_and_resume_stop_and_restart_scanning_without_losing_the_facts(
        tmp_path, monkeypatch):
    """Pause is the third verb, and it must differ from revoke in exactly one way: the
    facts stay. Driven through the shipped commands against the real probe."""
    from systemu.interface.cli_commands import (run_census_grant, run_census_pause,
                                                run_census_resume)

    root = _cloud_machine(tmp_path, monkeypatch)
    vault_dir = tmp_path / "vault"
    v = _vault(vault_dir)
    assert run_census_grant(v, "cloud_sync_roots", assume_yes=True) == 0
    assert ac.run_census(v)["scanned"] == ["cloud_sync_roots"]
    assert len(FactStore(v).all_facts()) == 1

    assert run_census_pause(v, "cloud_sync_roots") == 0
    paused = ac.run_census(v, min_interval_seconds=0)
    assert paused["skipped"]["cloud_sync_roots"] == "not_consented"
    assert [f.value for f in FactStore(v).all_facts()] == [str(root)], \
        "pause is not revoke — the facts stay"
    assert cc.CensusConsentStore(vault_dir).is_granted("cloud_sync_roots") is True

    assert run_census_resume(v, "cloud_sync_roots") == 0
    assert ac.run_census(v, min_interval_seconds=0)["scanned"] == ["cloud_sync_roots"]


def test_census_status_lists_every_category_and_its_reachability(tmp_path, monkeypatch,
                                                                 capsys):
    """``census status`` is the "see" half of M3 and the only place an operator learns what
    this build can and cannot do. It must show all three categories — not just granted
    ones, which would make an unconsented install look like the feature does not exist —
    and it must say plainly which are grantable from this build."""
    from systemu.interface.cli_commands import run_census_grant, run_census_status

    _cloud_machine(tmp_path, monkeypatch)
    v = _vault(tmp_path / "v")
    assert run_census_status(v) == 0
    fresh = capsys.readouterr().out
    for category in cc.CATEGORIES:
        assert category in fresh, category
    assert "not yet grantable" in fresh.lower()
    assert "not granted" in fresh.lower()

    assert run_census_grant(v, "cloud_sync_roots", assume_yes=True) == 0
    capsys.readouterr()
    assert run_census_status(v) == 0
    after = capsys.readouterr().out
    assert "cloud_sync_roots" in after and "granted" in after.lower()


def test_the_shipped_category_card_no_longer_disclaims_its_own_controls():
    """RE-AUDIT ITEM 2. ``revocation_surface_shipped`` is per-category now: true for the
    category whose controls actually ship, false for the two whose do not. Both halves
    are asserted — a blanket flip to True would be the same overclaim R-W2 was held for,
    and leaving it False for the shipped category would understate a control the operator
    has (and the prose would then contradict the CLI they just used)."""
    shipped = cc.consent_card("cloud_sync_roots")
    assert shipped["revocation_surface_shipped"] is True
    assert shipped["revocable"] is True
    blob = (shipped["revocation_notice"] + shipped["standing_scan_notice"]).lower()
    assert "no command" not in blob and "not built" not in blob, (
        f"the shipped category's card still tells the operator the controls do not "
        f"exist: {blob!r}")
    assert "census revoke" in blob, "the card must name the command that withdraws it"

    for category in set(cc.CATEGORIES) - set(cc.SURFACED_CATEGORIES):
        card = cc.consent_card(category)
        assert card["revocation_surface_shipped"] is False, category
        assert card["revocable"] is True, category      # the mechanism exists either way
