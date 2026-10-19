"""S5 / R-A3 -- the profile's ``keyring_backend`` row is truthful.

THE DEFECT
    ``keyring_backend`` reported ``dpapi`` on Windows whenever ANY usable keyring
    existed, even with the DPAPI at-rest envelope broken -- while the
    ``secrets_at_rest`` row (2d) said the secret files were plaintext. Two rows
    of one profile disagreed about the same fact, and the older one over-claimed.

THE RULING
    ``keyring_backend`` reads ``dpapi`` only when the envelope round trip works,
    from the SAME witness as ``secrets_at_rest`` (one derivation, no second
    probe). Otherwise it names the real backend -- the usable keyring, or the
    flagged ``plaintext_fallback``.

HERMETIC: the envelope and keyring seams are faked in every test here.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from systemu.runtime import platform_profile as pp


def WORKING():
    return (True, "")


def BROKEN():
    return (False, "the DPAPI envelope is not working (test)")


def _real():
    return object()


def _none():
    return None


def _keyring_whose_backend_lives_in(module: str):
    """A keyring-module stand-in whose ``get_keyring()`` backend reports
    ``module`` as its home (keyring's own layout: ``keyring.backends.<name>``)."""
    backend_cls = type("Keyring", (), {})
    backend_cls.__module__ = module
    return SimpleNamespace(get_keyring=lambda: backend_cls())


# --------------------------------------------------------------------------- #
# the Windows leg: dpapi only off a working envelope
# --------------------------------------------------------------------------- #

def test_a_usable_keyring_no_longer_earns_dpapi_without_a_working_envelope():
    assert pp.keyring_backend("win32", usable=_real, envelope=WORKING) == "dpapi"
    got = pp.keyring_backend("win32", usable=_real, envelope=BROKEN)
    assert got != "dpapi"
    # The real backend: Windows Credential Manager, the keyring a Windows host ships.
    assert got == pp.KEYRING_WINCRED == "wincred"


def test_no_keyring_and_no_envelope_is_the_flagged_plaintext_fallback():
    assert pp.keyring_backend("win32", usable=_none, envelope=BROKEN) == "plaintext_fallback"
    assert pp.keyring_backend("win32", usable=_none, envelope=WORKING) == "dpapi"


def test_the_legacy_dpapi_seam_is_the_same_envelope_witness():
    """``dpapi=`` (a bool witness) is kept for existing callers; it feeds the
    one derivation, so a usable keyring can no longer stand in for it."""
    assert pp.keyring_backend("win32", usable=_real, dpapi=lambda: True) == "dpapi"
    assert pp.keyring_backend("win32", usable=_real, dpapi=lambda: False) == "wincred"


@pytest.mark.parametrize("module, expected", [
    ("keyring.backends.Windows", "wincred"),
    ("keyring.backends.macOS", "keychain"),
    ("keyring.backends.SecretService", "secretservice"),
    ("keyring.backends.libsecret", "secretservice"),
    ("keyring.backends.kwallet", "secretservice"),
    ("some.third_party.backend", "wincred"),
])
def test_a_windows_leg_without_the_envelope_names_the_keyring_in_hand(module, expected):
    """The row names the keyring object it was handed, so a simulated Windows
    leg on a macOS runner says ``keychain`` -- never a backend that is not there."""
    kr = _keyring_whose_backend_lives_in(module)
    assert pp.keyring_backend("win32", usable=lambda: kr, envelope=BROKEN) == expected
    # ...and a working envelope still reads dpapi whatever the keyring is.
    assert pp.keyring_backend("win32", usable=lambda: kr, envelope=WORKING) == "dpapi"


def test_posix_rows_are_unchanged_by_the_envelope():
    for envelope in (WORKING, BROKEN):
        assert pp.keyring_backend("darwin", usable=_real, envelope=envelope) == "keychain"
        assert pp.keyring_backend("linux", usable=_real, envelope=envelope) == "secretservice"
        assert pp.keyring_backend("linux", usable=_none,
                                  envelope=envelope) == "plaintext_fallback"


# --------------------------------------------------------------------------- #
# one derivation: the profile probes the envelope ONCE and both rows agree
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("working", [True, False])
@pytest.mark.parametrize("has_keyring", [True, False])
def test_the_profile_derives_both_rows_from_one_envelope_probe(monkeypatch, working, has_keyring):
    calls = []

    def _probe():
        calls.append(1)
        return (True, "") if working else (False, "the probe says no (test)")

    monkeypatch.setattr(pp, "_envelope_status", _probe)
    monkeypatch.setattr(pp, "_usable_keyring", _real if has_keyring else _none)
    prof = pp.platform_profile(platform_str="win32", in_container=False,
                               provider_configured=True)

    assert len(calls) == 1, f"the envelope was probed {len(calls)} times for one profile"
    kr, sec = prof["keyring_backend"], prof["secrets_at_rest"]["backend"]
    assert (kr == "dpapi") is working and (sec == "dpapi") is working, (kr, sec)
    if not working:
        assert kr == ("wincred" if has_keyring else "plaintext_fallback")


def test_posix_profiles_never_spend_the_envelope_probe(monkeypatch):
    calls = []

    def _probe():
        calls.append(1)
        return (True, "")

    monkeypatch.setattr(pp, "_envelope_status", _probe)
    monkeypatch.setattr(pp, "_usable_keyring", _real)
    for p in ("darwin", "linux"):
        prof = pp.platform_profile(platform_str=p, in_container=False,
                                   provider_configured=True)
        assert prof["keyring_backend"] in ("keychain", "secretservice")
    assert calls == []


def test_doctor_keyring_row_follows_the_envelope(monkeypatch):
    monkeypatch.setattr(pp, "_envelope_status", BROKEN)
    monkeypatch.setattr(pp, "_usable_keyring", _real)
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True,
                               platform_str="win32", in_container=False)
    assert r["keyring"]["backend"] == "wincred"
    assert r["profile"]["secrets_at_rest"]["backend"] == "keyring"
    assert any(p["id"] == "secrets_envelope_unavailable" for p in r["problems"])
    # A usable Credential Manager is not "no OS keyring": no plaintext-fallback row.
    assert not any(p["id"] == "keyring_plaintext_fallback" for p in r["problems"])
