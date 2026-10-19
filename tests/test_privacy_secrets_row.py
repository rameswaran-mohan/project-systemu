"""S5 / R-A3 -- /privacy's "Secrets at rest" line reads the TRUTHFUL row.

THE DEFECT
    The line classified secrets as encrypted off ``keyring_backend`` -- the row
    that read ``dpapi`` on Windows whenever any keyring existed -- not off the
    ``secrets_at_rest`` row, the one derived from a WORKING envelope round trip.
    A broken envelope rendered as "held in the OS secret store (dpapi)".

THE RULING
    The secrets line's status, severity and claim come from
    ``profile["secrets_at_rest"]`` (never ``keyring_backend``). The pins below
    hand the report a ``keyring_backend`` that contradicts the row, so a line
    that reads the wrong one goes red.

HERMETIC: profiles are fixtures, or the live profile with the envelope and
keyring seams faked. No provider probe, no network.
"""
from __future__ import annotations

import pytest

from systemu.runtime import platform_profile as pp
from systemu.runtime import privacy

_REMOTE = "deepseek/deepseek-v4-flash"
_TIERS = {"tier1": _REMOTE, "tier2": _REMOTE, "tier3": _REMOTE}

ROW_DPAPI = {"backend": "dpapi", "label": "dpapi", "reason": ""}
_WIN_KEYRING_REASON = ("DPAPI envelope unavailable: win32crypt (pywin32) is not "
                       "importable; credentials are in the OS keyring, but OAuth "
                       "tokens and dashboard secrets are stored as plaintext files")
ROW_WIN_KEYRING = {"backend": "keyring", "label": "keyring",
                   "reason": _WIN_KEYRING_REASON}
ROW_POSIX_KEYRING = {"backend": "keyring", "label": "keyring", "reason": ""}
_PLAIN_REASON = ("DPAPI envelope unavailable: win32crypt (pywin32) is not "
                 "importable; no OS keyring backend")
ROW_PLAIN = {"backend": "plaintext", "label": f"plaintext ({_PLAIN_REASON})",
             "reason": _PLAIN_REASON}


def _profile(row, keyring_backend):
    return dict(os="win32", os_family="windows", arch="x86_64",
                python_version="3.12.0", capture_available=True,
                keyring_backend=keyring_backend, secrets_at_rest=row,
                forged_net_jail="absent", docker_mode=False,
                provider_configured=True, host_capabilities=[])


def _secrets(report):
    sec = [s for s in report["sections"] if s["key"] == "secrets"]
    assert len(sec) == 1
    return sec[0]


def _line(row, keyring_backend):
    return _secrets(privacy.privacy_report(profile=_profile(row, keyring_backend),
                                           tier_models=_TIERS))


# --------------------------------------------------------------------------- #
# the row decides; a contradicting keyring row cannot
# --------------------------------------------------------------------------- #

def test_a_plaintext_row_is_flagged_even_when_the_keyring_row_says_dpapi():
    # THE DEFECT, as data: the old keyring row over-claimed `dpapi`.
    s = _line(ROW_PLAIN, "dpapi")
    assert s["status"] == "plaintext" and s["severity"] == "warn"
    assert ROW_PLAIN["label"] in s["detail"]
    assert "NOT encrypted" in s["detail"]
    assert "held in the OS secret store" not in s["detail"]


def test_a_windows_keyring_without_the_envelope_is_flagged_and_says_what_stays_plaintext():
    s = _line(ROW_WIN_KEYRING, "dpapi")
    assert s["status"] == "partial" and s["severity"] == "warn"
    assert _WIN_KEYRING_REASON in s["detail"]


def test_a_working_envelope_reads_encrypted_even_without_a_keyring():
    # Windows, no keyring, envelope working: the credential FILE is the envelope.
    s = _line(ROW_DPAPI, "plaintext_fallback")
    assert s["status"] == "encrypted" and s["severity"] == "ok"
    assert "(dpapi)" in s["detail"] and "DPAPI" in s["detail"]


def test_a_posix_keyring_row_keeps_the_os_store_claim():
    s = _line(ROW_POSIX_KEYRING, "keychain")
    assert s["status"] == "encrypted" and s["severity"] == "ok"
    assert "OS secret store" in s["detail"] and "keychain" in s["detail"]


@pytest.mark.parametrize("row", [ROW_DPAPI, ROW_WIN_KEYRING, ROW_POSIX_KEYRING, ROW_PLAIN])
def test_the_secrets_line_is_ascii_and_names_the_keyring(row):
    s = _line(row, "wincred")
    assert s["detail"].isascii()
    assert "OS keyring: wincred" in s["detail"]


@pytest.mark.parametrize("bad", [
    {"backend": "dpapi"},                                  # incomplete row
    {"backend": 1, "label": "dpapi", "reason": ""},        # wrong concrete type
    {"backend": "aes-maybe", "label": "x", "reason": ""},  # unknown backend
    "dpapi",                                               # not a row at all
])
def test_a_malformed_row_is_never_read_as_encrypted(bad):
    s = _line(bad, "dpapi")
    assert s["status"] != "encrypted" and s["severity"] == "warn", s


# --------------------------------------------------------------------------- #
# the page and the live profile
# --------------------------------------------------------------------------- #

def test_the_page_renders_the_row_not_the_keyring_claim():
    from systemu.interface.pages import privacy as page
    report = privacy.privacy_report(profile=_profile(ROW_PLAIN, "dpapi"),
                                    tier_models=_TIERS)
    model = page.privacy_page_model(report=report)
    sec = [s for s in model["sections"] if s["key"] == "secrets"][0]
    text = " ".join(sec["lines"])
    assert ROW_PLAIN["label"] in text and sec["severity"] == "warn"
    assert page.page_text(model).isascii()


@pytest.mark.parametrize("working", [True, False])
@pytest.mark.parametrize("has_keyring", [True, False])
def test_the_live_line_follows_the_live_row(monkeypatch, working, has_keyring):
    monkeypatch.setattr(pp, "_envelope_status",
                        lambda: (True, "") if working else (False, "envelope off (test)"))
    monkeypatch.setattr(pp, "_usable_keyring",
                        (lambda: object()) if has_keyring else (lambda: None))
    live = pp.platform_profile(provider_configured=False)
    row = live["secrets_at_rest"]
    s = _secrets(privacy.privacy_report(tier_models=_TIERS))   # NO profile injected

    ok = row["backend"] == "dpapi" or (row["backend"] == "keyring" and not row["reason"])
    assert (s["status"] == "encrypted") is ok, (row, s)
    assert (s["severity"] == "ok") is ok, (row, s)
    if row["reason"]:
        assert row["reason"] in s["detail"]
    assert live["keyring_backend"] in s["detail"]
