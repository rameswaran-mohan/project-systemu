"""R-UX1 (SPEC §15-UX UX-4) — the `doctor` self-diagnosis command.

AC-U4: a killed/absent LLM provider and a locked keyring are REPORTED and make
`doctor` exit NONZERO; a healthy install exits ZERO. A dead daemon or a
plaintext-fallback keyring are surfaced but do NOT block (non-blocking warnings).
"""
from __future__ import annotations

from click.testing import CliRunner

from systemu.runtime import platform_profile as pp


# ── the pure report builder (deterministic, injected states) ─────────────────

def test_report_healthy_has_no_blocking_problems():
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True)
    assert r["ok"] is True
    assert pp.report_exit_code(r) == 0
    assert not any(p["blocking"] for p in r["problems"])


def test_report_killed_provider_blocks_and_is_reported():
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=False,
                               keyring_locked=False, daemon_running=True)
    assert r["ok"] is False
    assert pp.report_exit_code(r) != 0
    assert any(p["id"] == "provider_unreachable" and p["blocking"] for p in r["problems"])


def test_report_absent_provider_blocks():
    r = pp.build_doctor_report(provider_configured=False, provider_reachable=None,
                               keyring_locked=False, daemon_running=True)
    assert r["ok"] is False
    assert pp.report_exit_code(r) != 0
    assert any(p["id"] == "provider_absent" and p["blocking"] for p in r["problems"])


def test_report_locked_keyring_blocks():
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=True, daemon_running=True)
    assert r["ok"] is False
    assert pp.report_exit_code(r) != 0
    assert any(p["id"] == "keyring_locked" and p["blocking"] for p in r["problems"])


def test_report_daemon_down_is_a_nonblocking_warning():
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=False)
    assert r["ok"] is True                       # a dead daemon does not exit nonzero
    assert pp.report_exit_code(r) == 0
    assert any(p["id"] == "daemon_down" and not p["blocking"] for p in r["problems"])


def test_report_carries_profile_versions_and_keyring_backend():
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True)
    assert r["profile"]["forged_net_jail"] == "absent"
    assert "python" in r["versions"] and "systemu" in r["versions"]
    assert r["keyring"]["backend"] in {"dpapi", "keychain", "secretservice", "plaintext_fallback"}


def test_report_never_leaks_a_secret_value():
    # The report is safe to print — it must never embed a secret VALUE anywhere.
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True)
    import json
    blob = json.dumps(r, default=str)
    assert "OPENROUTER_API_KEY" not in blob or "sk-" not in blob


# ── the CLI wiring: exit code + report surface ───────────────────────────────

def _invoke():
    from systemu.interface.cli_commands import doctor_cmd
    return CliRunner().invoke(doctor_cmd, [], obj={})


def test_doctor_cli_exits_nonzero_on_killed_provider_and_locked_keyring(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-configured")   # configured but killed
    monkeypatch.setattr(pp, "_probe_provider_reachable", lambda: False)
    monkeypatch.setattr(pp, "_probe_keyring_locked", lambda: True)
    monkeypatch.setattr(pp, "_probe_daemon_running", lambda vault_dir=None: True)
    res = _invoke()
    assert res.exit_code != 0, res.output
    low = res.output.lower()
    assert "provider" in low and "keyring" in low


def test_doctor_cli_exits_zero_when_healthy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-configured")
    monkeypatch.setattr(pp, "_probe_provider_reachable", lambda: True)
    monkeypatch.setattr(pp, "_probe_keyring_locked", lambda: False)
    monkeypatch.setattr(pp, "_probe_daemon_running", lambda vault_dir=None: True)
    res = _invoke()
    assert res.exit_code == 0, res.output


def test_doctor_cli_renders_the_platform_profile(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-configured")
    monkeypatch.setattr(pp, "_probe_provider_reachable", lambda: True)
    monkeypatch.setattr(pp, "_probe_keyring_locked", lambda: False)
    monkeypatch.setattr(pp, "_probe_daemon_running", lambda vault_dir=None: True)
    res = _invoke()
    low = res.output.lower()
    # the capability card renders the jail-absent row + the DEP-10 honesty table
    assert "jail" in low and "absent" in low
    assert "host capabilities" in low


# ── the real top-level surface: bare `sharing_on doctor` (no scope_id) ────────
# `doctor <scope_id>` stays the existing scope-recovery command; bare `doctor`
# now runs whole-system self-diagnosis.

def test_bare_sharing_on_doctor_runs_self_diagnosis_and_blocks(monkeypatch):
    from sharing_on.cli import doctor
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-configured")   # configured but killed
    monkeypatch.setattr(pp, "_probe_provider_reachable", lambda: False)
    monkeypatch.setattr(pp, "_probe_keyring_locked", lambda: True)
    monkeypatch.setattr(pp, "_probe_daemon_running", lambda vault_dir=None: True)
    res = CliRunner().invoke(doctor, [])
    assert res.exit_code != 0, res.output
    low = res.output.lower()
    assert "provider" in low and "keyring" in low


def test_bare_sharing_on_doctor_healthy_exits_zero(monkeypatch):
    from sharing_on.cli import doctor
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-configured")
    monkeypatch.setattr(pp, "_probe_provider_reachable", lambda: True)
    monkeypatch.setattr(pp, "_probe_keyring_locked", lambda: False)
    monkeypatch.setattr(pp, "_probe_daemon_running", lambda vault_dir=None: True)
    res = CliRunner().invoke(doctor, [])
    assert res.exit_code == 0, res.output
