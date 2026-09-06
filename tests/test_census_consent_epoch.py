"""The census consent file is bound to a consent GENERATION, not only to the vault.

THE WITNESSED DEFECT (human-style e2e of v0.10.27)
--------------------------------------------------
The R-W2 consent file carries an HMAC keyed by a secret derived from THIS vault, so a
file lifted from another vault (or hand-edited) is refused. But the key had no notion
of WHEN the operator answered, so one genuinely-signed file stayed valid forever::

    census grant cloud_sync_roots        # typed yes
    cp <vault>/census_consent.json /tmp/copy.json
    census revoke cloud_sync_roots       # "Revoked ... Facts deleted: 1"
    cp /tmp/copy.json <vault>/census_consent.json
    census status                        # "GRANTED, active"
    run_census                           # scans again

The operator's LAST ACT was a withdrawal and the machine was enumerated anyway. The
release copy says a stray file can never manufacture consent; a stray file that the
operator's own vault once signed is exactly a stray file.

WHAT THE FENCE CLAIMS, EXACTLY
------------------------------
A SINGLE restored consent file never revives a withdrawn consent. `revoke` advances a
monotonic epoch counter held in a census-owned sidecar under the vault's `secrets/`
directory, and the epoch is an input to the consent MAC's key, so every signature
issued before that withdrawal is stale from the instant it lands.

WHAT THE FENCE DOES NOT CLAIM (typed P under DEC-36)
----------------------------------------------------
Restoring BOTH the consent file AND the epoch sidecar replays, and so does restoring
the vault's session secret. Those are writes to the KEY-DOMAIN files themselves, which
is host write access inside systemu's own trust domain: in-process Python is ONE trust
domain, and a party that can rewrite the key material can mint a genuine signature in
two lines regardless. The same class covers DELETING the sidecar, which fails CLOSED
immediately (everything reads UNCONSENTED) but resets the generation counter, so a
LATER operator-typed grant re-creates epoch 0 and a copy signed at epoch 0 verifies
again. That residual is stated rather than papered over; the defended surface is
HOSTILE DATA and HONEST-CODE ACCIDENTS, which is where the witnessed defect lived.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime import ambient_census as ac
from systemu.runtime import census_consent as cc
from systemu.runtime import vault_root

_REPO = Path(__file__).resolve().parent.parent
_CONC_MAP = _REPO / "docs" / "CONC-MAP.md"
_OWNERSHIP_TEST = _REPO / "tests" / "test_conc_map_writer_ownership.py"

#: The CONC-MAP anchor this slice registers. One string, used by the row assertion and
#: by the writer-ownership assertion, so the two cannot drift apart.
EPOCH_CONC_MAP_ANCHOR = "**R-W2 census consent epoch** `secrets/census_consent.epoch`"


# -- fixtures: a REAL store, a REAL consent file and a REAL sidecar on disk ---

def _vault(tmp_path):
    """The shape both concrete vault types expose (`FactStore` reads `.root`, and
    `CensusConsentStore` is constructed from it)."""
    return SimpleNamespace(root=tmp_path)


def _spy_probe(values, calls):
    """A probe that RECORDS being called. The whole point of a consent gate is that an
    unconsented probe is never REACHED, which a fact count cannot distinguish from a
    probe that ran and found nothing."""
    def _probe(limit, budget):
        calls.append(limit)
        return list(values)
    return _probe


def _use_spy(monkeypatch, category, values, calls):
    monkeypatch.setitem(ac.PROBES, category,
                        (_spy_probe(values, calls), ac.PROBES[category][1]))


def _isolate_vault(monkeypatch, tmp_path):
    """Point the one vault-root mint at tmp_path.

    Every store below is handed `tmp_path` explicitly, so this is belt-and-braces: no
    test in this file may write into the worktree's packaged `systemu/vault/`.
    """
    monkeypatch.setenv(vault_root.VAULT_DIR_ENV, str(tmp_path))


def _consent_path(base) -> Path:
    return Path(base) / "census_consent.json"


def _read_raw_consent(base):
    return json.loads(_consent_path(base).read_text(encoding="utf-8"))


def _write_raw_consent(base, obj) -> None:
    """Place a hand-built consent file: the forger's capability, exactly."""
    Path(base).mkdir(parents=True, exist_ok=True)
    _consent_path(base).write_text(json.dumps(obj, indent=2), encoding="utf-8")


# == 1. the exact witnessed repro, driven through the runtime API ==============

def test_a_restored_pre_revoke_consent_file_does_not_revive_the_withdrawn_grant(
        tmp_path, monkeypatch):
    """THE E2E REPRO PIN, byte-for-byte: the operator's own signed consent file, saved
    before `census revoke` and copied back after it.

    Every phase carries its positive control in the same test. "is_active is False" is
    also what a completely broken consent store produces, so the rejected case alone
    cannot distinguish "the epoch refused this" from "nothing works here" (DEC-32: a pin
    asserting a value the failure path also produces is not a pin).
    """
    _isolate_vault(monkeypatch, tmp_path)
    calls: list = []
    _use_spy(monkeypatch, "cloud_sync_roots", ["C:/Users/x/OneDrive"], calls)
    v = _vault(tmp_path)

    # `census grant cloud_sync_roots` (typed yes), then a real scan.
    ac.grant_category(v, "cloud_sync_roots")
    assert ac.run_census(v)["scanned"] == ["cloud_sync_roots"]      # CONTROL
    assert len(calls) == 1, "positive control: a genuine grant reaches the probe"

    # `cp <vault>/census_consent.json /tmp/copy.json` -- the operator's own bytes.
    saved = _consent_path(tmp_path).read_bytes()

    # `census revoke cloud_sync_roots` -> "Revoked ... Facts deleted: 1"
    revoked = ac.revoke_category(v, "cloud_sync_roots")
    assert revoked["revoked"] is True
    assert revoked["facts_removed"] >= 1

    # `cp /tmp/copy.json <vault>/census_consent.json` -- the replay.
    _consent_path(tmp_path).write_bytes(saved)
    assert _consent_path(tmp_path).read_bytes() == saved, "the replay really is on disk"

    store = cc.CensusConsentStore(tmp_path)
    assert store.is_active("cloud_sync_roots") is False, (
        "a consent file saved BEFORE the withdrawal was honoured after it -- the MAC "
        "binds the file to the vault but not to a consent generation, so one copy "
        "revives a withdrawn grant forever")
    assert store.is_granted("cloud_sync_roots") is False
    assert store.list_grants() == [], "the whole file must fail closed, not partly"

    # `census status` must not report GRANTED, and `run_census` must not scan.
    assert ac.census_status(v) == []
    summary = ac.run_census(v, min_interval_seconds=0)
    assert summary["skipped"].get("cloud_sync_roots") == "not_consented", summary
    assert len(calls) == 1, "the restored file reached a probe -- the replay scanned"
    assert summary["facts_written"] == 0


# == 2. the epoch is monotonic, and only a withdrawal advances it ==============

def test_the_consent_epoch_advances_on_revoke_and_on_nothing_else(tmp_path, monkeypatch):
    """`grant` / `pause` / `resume` / `mark_ran` record or suspend an answer; only
    `revoke` WITHDRAWS one, so only `revoke` may stale out every signature issued so
    far. A grant that bumped the epoch would invalidate the operator's other live
    grants; a revoke that did not would be the defect this slice closes."""
    _isolate_vault(monkeypatch, tmp_path)
    store = cc.CensusConsentStore(tmp_path)
    assert cc.read_consent_epoch(tmp_path) is None, "nothing on disk on a fresh vault"

    store.grant("cloud_sync_roots")
    first = cc.read_consent_epoch(tmp_path)
    assert first == 0, "the first signature is issued in generation 0"

    store.grant("cloud_sync_roots")                 # idempotent re-grant
    store.grant("path_clis")
    store.set_paused("path_clis", True)             # pause
    store.set_paused("path_clis", False)            # resume
    store.mark_ran("cloud_sync_roots")
    assert cc.read_consent_epoch(tmp_path) == first, (
        "a grant/pause/resume/mark_ran advanced the consent generation -- that would "
        "invalidate every OTHER live grant the operator still holds")
    assert store.is_active("cloud_sync_roots") is True          # CONTROL
    assert store.is_active("path_clis") is True                 # CONTROL

    assert store.revoke("cloud_sync_roots") is True
    assert cc.read_consent_epoch(tmp_path) == first + 1
    # The categories the operator did NOT withdraw survive the bump: `_write` re-signs
    # the remaining rows at the new generation.
    assert cc.CensusConsentStore(tmp_path).is_active("path_clis") is True, (
        "revoking one category invalidated the operator's other grants")

    assert store.revoke("path_clis") is True
    assert cc.read_consent_epoch(tmp_path) == first + 2

    # A no-op revoke leaves the counter alone: there is no signature to stale out, and
    # a counter that moved on every call would be a liveness signal on a privacy file.
    assert store.revoke("path_clis") is False
    assert cc.read_consent_epoch(tmp_path) == first + 2


def test_a_copy_taken_between_two_revokes_is_stale_at_every_later_generation(
        tmp_path, monkeypatch):
    """Monotonic, not merely different: a copy saved at generation N must stay refused
    across N+1, N+2, ... A counter that could return to a value an old file claims is
    not a generation counter."""
    _isolate_vault(monkeypatch, tmp_path)
    store = cc.CensusConsentStore(tmp_path)
    store.grant("cloud_sync_roots")
    store.grant("path_clis")
    saved = _consent_path(tmp_path).read_bytes()
    assert store.is_active("cloud_sync_roots") is True           # CONTROL

    store.revoke("cloud_sync_roots")
    for _ in range(3):
        _consent_path(tmp_path).write_bytes(saved)
        assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is False
        cc.CensusConsentStore(tmp_path).grant("path_clis")       # re-grant, then withdraw
        cc.CensusConsentStore(tmp_path).revoke("path_clis")
    _consent_path(tmp_path).write_bytes(saved)
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is False
    assert cc.CensusConsentStore(tmp_path).is_active("path_clis") is False


# == 3. a missing sidecar is never grandfathered ==============================

def test_a_consent_file_with_no_epoch_sidecar_is_unconsented(tmp_path, monkeypatch):
    """Same rule as the unsigned v1 format: nothing on disk can prove which generation
    an unanchored consent file belongs to, so it authorises nothing. Grandfathering it
    would hand the replay back -- delete one integer and every old copy verifies."""
    _isolate_vault(monkeypatch, tmp_path)
    store = cc.CensusConsentStore(tmp_path)
    store.grant("cloud_sync_roots")
    assert store.is_active("cloud_sync_roots") is True            # CONTROL
    sidecar = cc.consent_epoch_file(tmp_path)
    assert sidecar.exists(), "the grant must have anchored itself"

    sidecar.unlink()
    after = cc.CensusConsentStore(tmp_path)
    assert after.is_active("cloud_sync_roots") is False, (
        "a consent file with no epoch anchor was honoured -- deleting one integer "
        "must not grandfather every signature ever issued")
    assert after.is_granted("cloud_sync_roots") is False
    assert after.list_grants() == []
    assert cc.read_consent_epoch(tmp_path) is None, (
        "reading an unanchored consent file MINTED an anchor -- a read-only privacy "
        "check must never create the state it is checking")
    assert _consent_path(tmp_path).exists(), "the read must not rewrite the file either"


@pytest.mark.parametrize("junk", ["", "   ", "-1", "1.5", "0x10", "nine", "\u0669",
                                  "1 2", "99999999999999999999999999"])
def test_a_malformed_epoch_anchor_is_unconsented(tmp_path, monkeypatch, junk):
    """The anchor is ONE non-negative ASCII integer. Anything else is not an anchor, so
    it reads exactly like an absent one: fail closed, whole file."""
    _isolate_vault(monkeypatch, tmp_path)
    store = cc.CensusConsentStore(tmp_path)
    store.grant("cloud_sync_roots")
    assert store.is_active("cloud_sync_roots") is True            # CONTROL

    cc.consent_epoch_file(tmp_path).write_text(junk, encoding="utf-8")
    assert cc.read_consent_epoch(tmp_path) is None, junk
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is False, junk


def test_a_vault_with_neither_file_is_plain_fresh_state(tmp_path, monkeypatch):
    """No consent file and no anchor is a FRESH INSTALL, not an error: nothing is
    granted, nothing scans, and -- because `run_census` runs on every survey -- nothing
    at all is created in the vault by looking."""
    _isolate_vault(monkeypatch, tmp_path)
    v = _vault(tmp_path)
    assert cc.read_consent_epoch(tmp_path) is None
    assert ac.census_status(v) == []
    assert ac.run_census(v)["facts_written"] == 0
    assert cc.CensusConsentStore(tmp_path).list_grants() == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [], (
        "a census run on a fresh install created state in the vault -- the absent-file "
        "read must short-circuit before any anchor or secret is minted")


# == 4. the fences that already shipped must still hold =======================

def test_a_consent_file_and_anchor_from_another_vault_are_still_refused(tmp_path,
                                                                       monkeypatch):
    """The key stays PER-VAULT. Carrying the anchor across too must not help: two fresh
    vaults are both at generation 0, so if the epoch had REPLACED the per-vault binding
    instead of joining it, this transplant would now succeed."""
    _isolate_vault(monkeypatch, tmp_path)
    a, b = tmp_path / "vault_a", tmp_path / "vault_b"
    cc.CensusConsentStore(a).grant("cloud_sync_roots")
    cc.CensusConsentStore(b).grant("path_clis")
    assert cc.CensusConsentStore(a).is_active("cloud_sync_roots") is True   # CONTROL
    assert cc.CensusConsentStore(b).is_active("path_clis") is True          # CONTROL
    assert cc.read_consent_epoch(a) == cc.read_consent_epoch(b) == 0

    _consent_path(b).write_bytes(_consent_path(a).read_bytes())
    cc.consent_epoch_file(b).write_bytes(cc.consent_epoch_file(a).read_bytes())
    moved = cc.CensusConsentStore(b)
    assert moved.is_active("cloud_sync_roots") is False, (
        "a consent file signed by another vault's key was honoured")
    assert moved.list_grants() == []


def test_a_hand_edited_grant_is_still_refused_whole_file(tmp_path, monkeypatch):
    """The MAC still covers the whole grants object at the current generation, so an
    attacker cannot bolt a category onto a file the operator legitimately signed."""
    _isolate_vault(monkeypatch, tmp_path)
    store = cc.CensusConsentStore(tmp_path)
    store.grant("cloud_sync_roots")
    assert store.is_active("cloud_sync_roots") is True             # CONTROL

    signed = _read_raw_consent(tmp_path)
    signed["grants"]["installed_apps"] = dict(signed["grants"]["cloud_sync_roots"])
    _write_raw_consent(tmp_path, signed)
    after = cc.CensusConsentStore(tmp_path)
    assert after.is_active("installed_apps") is False
    assert after.is_active("cloud_sync_roots") is False, (
        "a tampered file kept authorising the untouched rows -- 'partly valid' is not "
        "a state this store may produce")


def test_an_unsigned_v1_file_is_still_never_grandfathered(tmp_path, monkeypatch):
    """The pre-authenticator format stays UNCONSENTED, anchor present or not."""
    _isolate_vault(monkeypatch, tmp_path)
    store = cc.CensusConsentStore(tmp_path)
    store.grant("cloud_sync_roots")
    signed = _read_raw_consent(tmp_path)
    assert store.is_active("cloud_sync_roots") is True             # CONTROL
    assert cc.read_consent_epoch(tmp_path) == 0

    _write_raw_consent(tmp_path, {"version": 1, "grants": signed["grants"]})
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is False
    assert _read_raw_consent(tmp_path)["version"] == 1, "the read rewrote the file"


def test_a_genuine_grant_is_accepted_and_scans(tmp_path, monkeypatch):
    """The load-bearing positive control for this whole file: the epoch must not have
    turned the consent surface into a permanently-closed gate."""
    _isolate_vault(monkeypatch, tmp_path)
    calls: list = []
    _use_spy(monkeypatch, "cloud_sync_roots", ["C:/Users/x/OneDrive"], calls)
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is True
    assert ac.run_census(v)["scanned"] == ["cloud_sync_roots"]
    assert calls == [ac.MAX_ENTRIES_PER_CATEGORY]
    assert [g["category"] for g in ac.census_status(v)] == ["cloud_sync_roots"]

    # ...and it survives a daemon restart (a fresh store object, same disk).
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is True
    assert ac.run_census(v, min_interval_seconds=0)["scanned"] == ["cloud_sync_roots"]


def test_every_consent_mutation_including_the_epoch_bump_holds_the_rmw_lock(
        tmp_path, monkeypatch):
    """`_CONSENT_LOCK` still wraps the whole read-modify-write, and the epoch bump joins
    it rather than sitting outside it.

    A bump outside the lock is the same class of defect the lock was added for: a
    `mark_ran` that loaded before the bump and signed after it would re-sign a revoked
    grant into the NEW generation, which is a resurrection the epoch itself cannot
    catch. Asserted at the WRITE, not by racing threads: dropping the lock from ONE
    mutator is exactly the change a stress test would miss.
    """
    _isolate_vault(monkeypatch, tmp_path)
    store = cc.CensusConsentStore(tmp_path)
    writes: list = []
    bumps: list = []
    real_write = cc.CensusConsentStore._write
    real_bump = cc._bump_consent_epoch

    def _spy_write(self, grants):
        writes.append(cc._CONSENT_LOCK.locked())
        return real_write(self, grants)

    def _spy_bump(base_dir):
        bumps.append(cc._CONSENT_LOCK.locked())
        return real_bump(base_dir)

    monkeypatch.setattr(cc.CensusConsentStore, "_write", _spy_write)
    monkeypatch.setattr(cc, "_bump_consent_epoch", _spy_bump)
    store.grant("installed_apps")
    store.set_paused("installed_apps", True)
    store.mark_ran("installed_apps")
    store.revoke("installed_apps")
    assert writes == [True, True, True, True], \
        "grant / set_paused / mark_ran / revoke must each hold the consent lock"
    assert bumps == [True], (
        "revoke must bump the consent epoch exactly once, INSIDE the lock it already "
        f"holds for the rewrite; saw {bumps}")


# == 5. the new durable writer is registered (CONC-MAP + ownership guard) ======

def _ownership_module():
    spec = importlib.util.spec_from_file_location(
        "_conc_map_ownership_for_epoch", _OWNERSHIP_TEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_epoch_sidecar_is_registered_as_a_guarded_durable_writer():
    """A new durable store with no CONC-MAP entry is a DEC-10 review that never
    happened. Registered on the BUMP call, which is the one operation that can strand
    every live signature -- and, unlike the store constructor, it is a reachability pin:
    delete the bump from `revoke` and the ownership guard's "missing writer" half goes
    red."""
    ownership = _ownership_module()
    entries = [spec for spec in ownership.WRITER_OWNERSHIP.values()
               if spec.get("conc_map_row") == EPOCH_CONC_MAP_ANCHOR]
    assert len(entries) == 1, (
        "the census consent EPOCH sidecar has no writer-ownership entry anchored on "
        f"{EPOCH_CONC_MAP_ANCHOR!r} in tests/test_conc_map_writer_ownership.py")
    spec = entries[0]
    assert spec["call"] == "_bump_consent_epoch("
    assert spec["allowed"] == {"runtime/census_consent.py"}, (
        "the epoch anchor must have exactly ONE writer: the consent store itself")


def test_the_epoch_sidecar_has_its_own_conc_map_row():
    text = _CONC_MAP.read_text(encoding="utf-8", errors="replace")
    assert len(text) > 2000, "CONC-MAP.md is unexpectedly small -- is the path right?"
    assert text.count(EPOCH_CONC_MAP_ANCHOR) == 1, (
        f"{EPOCH_CONC_MAP_ANCHOR!r} appears {text.count(EPOCH_CONC_MAP_ANCHOR)} time(s) "
        "in docs/CONC-MAP.md (expected exactly 1)")
    # The row it belongs to must not swallow the EXISTING consent-file row's anchor:
    # two rows, two anchors, so the ownership guard can tell them apart.
    assert text.count("| **R-W2 census consent** `census_consent.json`") == 1


def test_the_epoch_sidecar_lives_under_the_already_ignored_secrets_dir():
    """The anchor is runtime state, so it must land where the packaged-seed fence and
    .gitignore already cover it (`<vault>/secrets/`) rather than at the vault root."""
    rel = cc.consent_epoch_file("VAULTROOT")
    assert rel.parent.name == "secrets", rel
    assert rel.name == cc.CONSENT_EPOCH_FILENAME
    ignore = (_REPO / ".gitignore").read_text(encoding="utf-8", errors="replace")
    assert "systemu/vault/secrets/" in ignore
