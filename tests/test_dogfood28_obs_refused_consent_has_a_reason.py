"""OBS -- a rolled-back census consent file is refused SILENTLY.

THE WITNESSED BEHAVIOUR (dogfood 28, replaying the v0.10.27 repro)
------------------------------------------------------------------
    census grant cloud_sync_roots        # typed yes
    cp <vault>/census_consent.json /tmp/copy.json
    census revoke cloud_sync_roots       # the epoch advances
    cp /tmp/copy.json <vault>/census_consent.json
    census status

The fence WORKS -- the restored file authenticates against a key that no longer
exists, nothing is granted, and no probe runs. That half is not in question.

What is: `census status` renders the restored vault IDENTICALLY to a vault that
was never granted at all. Both say "not granted" for every category and stop.
An operator looking at a file they just put back, being told nothing about it,
reasonably concludes the command did not see it -- and the next thing they try
is putting it back again, or hand-editing it. The refusal is correct and
invisible, which is the one combination that teaches the operator to fight it.

THE PROPERTY PINNED HERE
    A consent file that EXISTS but was REFUSED says so, with the reason:

        a consent file exists but was refused (stale generation); nothing is
        granted - re-run census grant if you meant it

    The reason is a VALUE on the runtime verdict, never a raise across the
    boundary (DEC-32): every existing caller of the load path still gets the
    same empty grants it always got, and the reason rides beside them.

    The three ruled reasons are distinguished, and the specific one is WITNESSED
    rather than inferred (DEC-27): "stale generation" is claimed ONLY when the
    file's MAC actually verifies against a key from an earlier generation of
    THIS vault. A MAC that verifies nowhere is a "signature mismatch"; an
    unreadable generation anchor is "no generation anchor". Two further reasons
    exist because the alternative was a false statement about the file: an
    unreadable/unparseable/unsigned file, and a vault whose signing secret could
    not be obtained (so nothing was checked at all).

    The bounded generation search is DISPLAY work and is asked for only by the
    operator-facing read -- `run_census` gates on the same load per category per
    survey, and must not pay for a diagnosis nobody reads.

NOTHING HERE WRITES INTO THE WORKTREE'S VAULT. Every store is handed an
explicit tmp_path, and the one vault-root mint is pointed at it as well.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime import ambient_census as ac
from systemu.runtime import census_consent as cc
from systemu.runtime import vault_root


CATEGORY = "cloud_sync_roots"


def _vault(tmp_path):
    return SimpleNamespace(root=tmp_path)


def _isolate_vault(monkeypatch, tmp_path):
    """Point the one vault-root mint at tmp_path. Belt-and-braces: every store
    below is handed tmp_path explicitly."""
    monkeypatch.setenv(vault_root.VAULT_DIR_ENV, str(tmp_path))


def _consent_path(base) -> Path:
    return Path(base) / "census_consent.json"


def _granted_vault(tmp_path, monkeypatch):
    """A vault where the operator really did type yes, with the signed bytes
    saved off to the side the way the repro saves them."""
    _isolate_vault(monkeypatch, tmp_path)
    ac.grant_category(_vault(tmp_path), CATEGORY)
    saved = _consent_path(tmp_path).read_bytes()
    assert cc.CensusConsentStore(tmp_path).is_active(CATEGORY), \
        "precondition: the grant did not take, so nothing below tests a refusal"
    return saved


# --------------------------------------------------------------------------- #
# 1. the exact replay repro -> "stale generation"
# --------------------------------------------------------------------------- #

def test_a_restored_pre_revoke_file_is_refused_WITH_A_REASON(tmp_path,
                                                             monkeypatch):
    """THE REPRO. grant -> copy -> revoke -> restore -> status."""
    saved = _granted_vault(tmp_path, monkeypatch)
    ac.revoke_category(_vault(tmp_path), CATEGORY)
    _consent_path(tmp_path).write_bytes(saved)

    store = cc.CensusConsentStore(tmp_path)
    verdict = store.consent_file_status()

    assert verdict.refused is True, verdict
    assert verdict.reason == cc.REFUSED_STALE_GENERATION, verdict.reason
    assert verdict.grants == {}, verdict.grants
    # the fence itself, restated here so a "reason" that arrived alongside a
    # revived grant could never read as a pass
    assert store.is_active(CATEGORY) is False


def test_the_status_line_names_the_stale_generation(tmp_path, monkeypatch):
    """The operator-facing sentence, on the repro's own vault."""
    saved = _granted_vault(tmp_path, monkeypatch)
    ac.revoke_category(_vault(tmp_path), CATEGORY)
    _consent_path(tmp_path).write_bytes(saved)

    line = cc.census_status_refusal_line(tmp_path)

    assert "a consent file exists but was refused" in line
    assert "stale generation" in line
    assert "nothing is granted" in line
    assert "census grant" in line
    line.encode("ascii")


# --------------------------------------------------------------------------- #
# 2. the other two reasons, and the silent case that must STAY silent
# --------------------------------------------------------------------------- #

def test_a_hand_edited_file_reads_as_a_signature_mismatch(tmp_path,
                                                          monkeypatch):
    """The forger's capability: one file placed in the vault. It never verified
    at ANY generation, so claiming "stale generation" here would be a guess
    dressed as a diagnosis."""
    _granted_vault(tmp_path, monkeypatch)
    data = json.loads(_consent_path(tmp_path).read_text(encoding="utf-8"))
    data["grants"]["installed_apps"] = {"granted_at": "2026-01-01T00:00:00+00:00"}
    _consent_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")

    verdict = cc.CensusConsentStore(tmp_path).consent_file_status()

    assert verdict.refused is True, verdict
    assert verdict.reason == cc.REFUSED_SIGNATURE_MISMATCH, verdict.reason
    assert cc.census_status_refusal_line(tmp_path).count("signature mismatch") == 1


def test_a_consent_file_with_no_anchor_says_so(tmp_path, monkeypatch):
    """Deleting the generation sidecar fails closed at once -- and it is a
    DIFFERENT thing from a bad signature: nothing on disk can say which
    generation the file belongs to, so no signature question was even reached.
    """
    _granted_vault(tmp_path, monkeypatch)
    cc.consent_epoch_file(tmp_path).unlink()

    verdict = cc.CensusConsentStore(tmp_path).consent_file_status()

    assert verdict.refused is True, verdict
    assert verdict.reason == cc.REFUSED_NO_GENERATION_ANCHOR, verdict.reason
    assert "no generation anchor" in cc.census_status_refusal_line(tmp_path)


def test_a_fresh_vault_says_nothing_at_all(tmp_path, monkeypatch):
    """THE CONTROL, and the reason this is a line rather than a banner: an
    install that was never granted has no consent file, nothing was refused,
    and there is nothing to report. A line here would be noise on every single
    `census status` an operator ever runs."""
    _isolate_vault(monkeypatch, tmp_path)

    verdict = cc.CensusConsentStore(tmp_path).consent_file_status()

    assert verdict.refused is False, verdict
    assert verdict.reason == "", verdict.reason
    assert cc.census_status_refusal_line(tmp_path) == ""


def test_a_live_granted_vault_says_nothing_either(tmp_path, monkeypatch):
    """The other control: a file that VERIFIES is not a refusal."""
    _granted_vault(tmp_path, monkeypatch)

    verdict = cc.CensusConsentStore(tmp_path).consent_file_status()

    assert verdict.refused is False, verdict
    assert verdict.grants, "the granted rows must still come back"
    assert cc.census_status_refusal_line(tmp_path) == ""


def test_a_clean_revoke_leaves_nothing_to_complain_about(tmp_path, monkeypatch):
    """`census revoke` rewrites and re-signs the file at the NEW generation, so
    an operator who simply revoked must not be told their consent file was
    refused. This is the false-positive that would make the line untrustworthy.
    """
    _granted_vault(tmp_path, monkeypatch)
    ac.revoke_category(_vault(tmp_path), CATEGORY)

    assert cc.census_status_refusal_line(tmp_path) == "", \
        "a normal revoke must not report a refused consent file"


def test_an_unsigned_or_corrupt_file_is_still_reported(tmp_path, monkeypatch):
    """The v1 unsigned format, and outright rubbish. Neither is one of the three
    named reasons, and neither may be silent: the file is THERE and it is not
    being honoured, which is the thing the operator cannot otherwise see."""
    _isolate_vault(monkeypatch, tmp_path)
    Path(tmp_path).mkdir(parents=True, exist_ok=True)

    for payload in ('{"version": 1, "grants": {"cloud_sync_roots": {}}}',
                    "not json at all",
                    '[]'):
        _consent_path(tmp_path).write_text(payload, encoding="utf-8")
        verdict = cc.CensusConsentStore(tmp_path).consent_file_status()
        assert verdict.refused is True, (payload, verdict)
        assert verdict.reason == cc.REFUSED_MALFORMED, (payload, verdict.reason)
        assert cc.census_status_refusal_line(tmp_path), payload


# --------------------------------------------------------------------------- #
# 3. the shape of the fence: a value, not a raise; and the old contract intact
# --------------------------------------------------------------------------- #

def test_the_refusal_reason_never_crosses_the_boundary_as_an_exception(
        tmp_path, monkeypatch):
    """DEC-32. Every one of these inputs used to return `{}` and must still: a
    caller that gained an exception here would be a `run_census` that started
    raising on a damaged consent file instead of quietly not scanning."""
    _isolate_vault(monkeypatch, tmp_path)
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    store = cc.CensusConsentStore(tmp_path)

    for payload in ("", "{}", "[]", "not json", '{"version": 2}',
                    '{"version": 2, "grants": {}, "mac": 7}',
                    '{"version": 2, "grants": [], "mac": "aa"}',
                    '{"version": 2, "grants": {}, "mac": "é"}'):
        _consent_path(tmp_path).write_text(payload, encoding="utf-8")
        assert store._load() == {}, payload
        assert store.is_active(CATEGORY) is False, payload
        assert store.list_grants() == [], payload
        verdict = store.consent_file_status()
        assert verdict.grants == {}, payload
        assert type(verdict.reason) is str, payload


def test_the_helper_is_pure_and_refuses_a_foreign_value():
    """The renderer is a small pure function over the verdict, so the CLI's call
    site is one line and this file can pin the SENTENCE without a CLI. A value
    of the wrong type renders nothing rather than formatting whatever `repr`
    happens to produce (DEC-36: the concrete type is pinned before use)."""
    assert cc.refusal_line(cc.ConsentFileVerdict(
        grants={}, refused=True,
        reason=cc.REFUSED_STALE_GENERATION)).startswith(
            "a consent file exists but was refused")
    assert cc.refusal_line(cc.ConsentFileVerdict(grants={}, refused=False)) == ""
    assert cc.refusal_line(None) == ""
    assert cc.refusal_line(SimpleNamespace(refused=True, reason="anything")) == ""


def test_the_line_is_ascii_for_every_reason():
    """It reaches ConPTY on the same console every other v0.10.29/0.10.30
    verdict surface was made ASCII for (DEC-32c)."""
    for reason in (cc.REFUSED_STALE_GENERATION, cc.REFUSED_SIGNATURE_MISMATCH,
                   cc.REFUSED_NO_GENERATION_ANCHOR, cc.REFUSED_MALFORMED):
        line = cc.refusal_line(cc.ConsentFileVerdict(grants={}, refused=True,
                                                     reason=reason))
        line.encode("ascii")
        assert reason in line, (reason, line)


def test_reading_a_refused_file_still_mints_no_vault_secret_on_a_fresh_install(
        tmp_path, monkeypatch):
    """The property the load path already had, re-pinned across the new read:
    a status check on a vault with NO consent file must not create the vault
    secret (or the anchor) as a side effect of asking."""
    _isolate_vault(monkeypatch, tmp_path)
    Path(tmp_path).mkdir(parents=True, exist_ok=True)

    cc.CensusConsentStore(tmp_path).consent_file_status()
    cc.census_status_refusal_line(tmp_path)

    assert not cc.consent_epoch_file(tmp_path).exists(), \
        "the read created the generation anchor"
    assert not (Path(tmp_path) / "secrets").exists(), \
        "the read created the vault's secrets directory"


def test_the_diagnosis_never_hands_back_the_stale_grants(tmp_path, monkeypatch):
    """The load-bearing half. Finding the earlier generation a file was signed
    at is a DIAGNOSIS, and a diagnosis that returned the rows it just refused
    would be the original defect with a friendlier message."""
    saved = _granted_vault(tmp_path, monkeypatch)
    ac.revoke_category(_vault(tmp_path), CATEGORY)
    _consent_path(tmp_path).write_bytes(saved)

    store = cc.CensusConsentStore(tmp_path)
    verdict = store.consent_file_status()

    assert verdict.reason == cc.REFUSED_STALE_GENERATION
    assert verdict.grants == {}
    assert store._load() == {}
    assert store.is_granted(CATEGORY) is False
    assert store.list_grants() == []
    assert ac.census_status(_vault(tmp_path)) == []


def test_a_file_from_a_far_older_generation_is_still_diagnosed(tmp_path,
                                                              monkeypatch):
    """Several withdrawals later, the same file is still recognisably a stale
    one. Bounded, deliberately -- the search back through generations is capped
    so a large counter cannot turn a status read into a key-derivation loop."""
    saved = _granted_vault(tmp_path, monkeypatch)
    for _ in range(5):
        ac.revoke_category(_vault(tmp_path), CATEGORY)
        ac.grant_category(_vault(tmp_path), CATEGORY)
    ac.revoke_category(_vault(tmp_path), CATEGORY)
    _consent_path(tmp_path).write_bytes(saved)

    verdict = cc.CensusConsentStore(tmp_path).consent_file_status()

    assert verdict.reason == cc.REFUSED_STALE_GENERATION, verdict.reason


def test_the_generation_search_is_bounded(tmp_path, monkeypatch):
    """The bound is a real number, and a file older than it degrades to the
    LESS specific claim rather than to a silent pass or an unbounded loop."""
    assert type(cc.MAX_DIAGNOSED_GENERATIONS) is int
    assert 0 < cc.MAX_DIAGNOSED_GENERATIONS <= 1024

    saved = _granted_vault(tmp_path, monkeypatch)
    # A no-op revoke deliberately does not advance the generation, so each
    # withdrawal is paired with a grant to give the next one something to
    # withdraw. Three real withdrawals: the saved file is three generations old.
    for _ in range(3):
        ac.revoke_category(_vault(tmp_path), CATEGORY)
        ac.grant_category(_vault(tmp_path), CATEGORY)
    assert cc.read_consent_epoch(tmp_path) == 3, cc.read_consent_epoch(tmp_path)
    _consent_path(tmp_path).write_bytes(saved)

    monkeypatch.setattr(cc, "MAX_DIAGNOSED_GENERATIONS", 1)
    verdict = cc.CensusConsentStore(tmp_path).consent_file_status()

    assert verdict.refused is True, verdict
    assert verdict.reason == cc.REFUSED_SIGNATURE_MISMATCH, verdict.reason


def test_the_scan_path_never_pays_for_the_diagnosis(tmp_path, monkeypatch):
    """The generation search is DISPLAY work, and `run_census` gates on the same
    load path per category per survey.

    Left on the fence path, a vault holding one stale consent file would derive
    up to `MAX_DIAGNOSED_GENERATIONS` keys -- each of them a vault-secret read --
    on every `is_active` call, forever. So the diagnosis is asked for only by
    the operator-facing read, and this pins that: the seam is counted, not
    assumed.
    """
    saved = _granted_vault(tmp_path, monkeypatch)
    ac.revoke_category(_vault(tmp_path), CATEGORY)
    _consent_path(tmp_path).write_bytes(saved)

    store = cc.CensusConsentStore(tmp_path)
    calls = []
    real = cc.CensusConsentStore._diagnose_mac_refusal
    monkeypatch.setattr(
        cc.CensusConsentStore, "_diagnose_mac_refusal",
        lambda self, epoch, grants, candidate: (
            calls.append(epoch) or real(self, epoch, grants, candidate)))

    assert store.is_active(CATEGORY) is False
    assert store._load() == {}
    assert store.list_grants() == []
    assert calls == [], "the scan path ran the display-only diagnosis"

    assert store.consent_file_status().reason == cc.REFUSED_STALE_GENERATION
    assert len(calls) == 1, calls
