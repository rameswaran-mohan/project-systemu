"""`systemu doctor` shows the verified secrets-at-rest row.

The platform profile mints ONE ``secrets_at_rest`` row (``dpapi`` only after a
real DPAPI round trip; ``keyring``; or ``plaintext (<reason>)``).  /privacy and
/health already read it; the `doctor` CLI table did not, so an operator running
the self-diagnosis saw a keyring row that said nothing about whether the secret
FILES were encrypted.

THE PROPERTY
    The real `doctor` command's "Platform capability profile" table carries a
    "Secrets at rest" row whose value is the mint's own ``label`` for the
    inputs the mint was given -- patched at the mint's seams (the envelope
    probe and the keyring probe), never at the row text.  Two different mint
    states render two different values, so the row cannot be a constant.

HERMETIC: provider, keyring-lock, daemon and backup probes are faked; nothing
touches a vault, a socket or the real OS credential store.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from systemu.interface import cli_commands
from systemu.runtime import platform_profile as pp

_ROW = "Secrets at rest"


def _ready_daemon() -> dict:
    """A daemon that is up and running the same build as this process."""
    import systemu
    own = str(Path(systemu.__file__).resolve().parent)
    return {"running": True, "ready": True, "pid": 1234, "process_alive": True,
            "host": "127.0.0.1", "port": 8765, "url": "http://127.0.0.1:8765",
            "reason": "accepting connections",
            "daemon_version": systemu.__version__, "daemon_path": own,
            "cli_version": systemu.__version__, "cli_path": own,
            "build_match": True,
            "build_note": f"same build on both sides: systemu {systemu.__version__}"}


def _arm(monkeypatch, *, platform_str: str, working: bool, has_keyring: bool):
    """Fake every probe; return the ``(usable, envelope)`` seams the mint reads.

    The platform is injected at the ``build_doctor_report`` call `doctor` makes,
    so the test means the same thing on a Windows host and on a POSIX CI leg.
    """
    def envelope():
        return (True, "") if working else (False, "envelope off (test)")

    def usable():
        return object() if has_keyring else None

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-configured")
    monkeypatch.setattr(pp, "_probe_provider_reachable", lambda: True)
    monkeypatch.setattr(pp, "_probe_keyring_locked", lambda: False)
    monkeypatch.setattr(pp, "_probe_daemon_state", lambda vault_dir=None: _ready_daemon())
    monkeypatch.setattr(pp, "_probe_secret_backups", lambda vault_dir=None: [])
    monkeypatch.setattr(pp, "_last_error", lambda: None)
    monkeypatch.setattr(pp, "_envelope_status", envelope)
    monkeypatch.setattr(pp, "_usable_keyring", usable)

    real_build = pp.build_doctor_report

    def build(**kwargs):
        kwargs.setdefault("platform_str", platform_str)
        return real_build(**kwargs)

    monkeypatch.setattr(pp, "build_doctor_report", build)
    # Wide enough that no cell wraps; the Console writes to whatever sys.stdout
    # is at print time, which is CliRunner's capture.
    monkeypatch.setattr(cli_commands, "console", Console(width=240))
    return usable, envelope


def _row_values(output: str) -> list:
    """Every value cell of a table row whose first cell is exactly the row key."""
    found = []
    for line in output.splitlines():
        cells = [cell.strip() for cell in re.split("[\u2502\u2503|]", line)]
        if _ROW in cells:
            found.append(cells[cells.index(_ROW) + 1])
    return found


def _doctor(monkeypatch, *, platform_str: str, working: bool, has_keyring: bool):
    """Run the real `doctor` command; return ``(shown, minted)``.

    ``shown`` is the rendered "Secrets at rest" cell; ``minted`` is the label of
    an independent call to the mint with the SAME seams, so the comparison is
    against the mint's value and never against a string written in this file.
    """
    usable, envelope = _arm(monkeypatch, platform_str=platform_str,
                            working=working, has_keyring=has_keyring)
    res = CliRunner().invoke(cli_commands.doctor_cmd, [], obj={})
    # `doctor` exits nonzero on a blocking problem; none of these states has one.
    assert res.exit_code == 0, res.output
    values = _row_values(res.output)
    assert len(values) == 1, (
        f"expected exactly one '{_ROW}' row, got {len(values)}\n{res.output}")
    minted = pp.secrets_at_rest(platform_str, usable=usable, envelope=envelope)["label"]
    return values[0], minted


def test_doctor_shows_dpapi_when_the_envelope_round_trip_works(monkeypatch):
    shown, minted = _doctor(monkeypatch, platform_str="win32",
                            working=True, has_keyring=True)
    assert minted == "dpapi"            # the mint, independently called
    assert shown == minted


def test_doctor_shows_keyring_when_the_envelope_does_not_work(monkeypatch):
    shown, minted = _doctor(monkeypatch, platform_str="win32",
                            working=False, has_keyring=True)
    assert minted == "keyring"
    assert shown == minted


def test_doctor_shows_the_plaintext_label_with_its_reason(monkeypatch):
    shown, minted = _doctor(monkeypatch, platform_str="win32",
                            working=False, has_keyring=False)
    assert minted.startswith("plaintext (")
    assert shown == minted
    assert shown.isascii()


def test_doctor_row_follows_the_mint_on_a_posix_leg(monkeypatch):
    shown, minted = _doctor(monkeypatch, platform_str="linux",
                            working=False, has_keyring=False)
    assert minted.startswith("plaintext (") and "0600" in minted
    assert shown == minted


def test_the_row_is_not_a_constant(monkeypatch):
    """Different mint inputs, different rendered values."""
    seen = set()
    for working, has_keyring in ((True, True), (False, True), (False, False)):
        shown, _minted = _doctor(monkeypatch, platform_str="win32",
                                 working=working, has_keyring=has_keyring)
        seen.add(shown)
    assert len(seen) == 3, seen


def test_the_bare_sharing_on_doctor_renders_the_same_row(monkeypatch):
    """`sharing_on doctor` (no scope id) runs the same renderer."""
    from sharing_on.cli import doctor
    usable, envelope = _arm(monkeypatch, platform_str="win32",
                            working=True, has_keyring=True)
    res = CliRunner().invoke(doctor, [])
    assert res.exit_code == 0, res.output
    assert _row_values(res.output) == [
        pp.secrets_at_rest("win32", usable=usable, envelope=envelope)["label"]]


# -- the pure reader: a row it cannot read is "unknown", never a claim --------

@pytest.mark.parametrize("profile", [
    {}, {"secrets_at_rest": None}, {"secrets_at_rest": {}},
    {"secrets_at_rest": {"label": ""}}, {"secrets_at_rest": {"label": 7}},
    "not a dict", None,
], ids=["no-row", "row-none", "row-empty", "label-empty", "label-int",
        "profile-str", "profile-none"])
def test_an_unreadable_row_reads_as_unknown(profile):
    text = cli_commands._doctor_secrets_text(profile)
    assert text.startswith("unknown")
    assert "encrypted" not in text and text != "dpapi"


def test_the_reader_renders_ascii_only():
    text = cli_commands._doctor_secrets_text(
        {"secrets_at_rest": {"label": "plaintext (caf\u00e9)"}})
    assert text.isascii()
    assert text.startswith("plaintext (caf")
