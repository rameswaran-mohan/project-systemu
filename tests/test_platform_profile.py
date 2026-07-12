"""R-UX1 (SPEC §15-UX UX-6 / §15-DEP DEP-1/6/10) — the ONE deterministic
cross-OS capability profile.

AC-U6: an IDENTICAL profile schema across win32/darwin/linux fixtures; the
jail-absent capability card renders; the container never pretends a host
capability is present (DEP-10 honesty rows read "available via Host Companion").

The profile is deterministic + hermetic: ``sys.platform`` is injected via the
``platform_str`` kwarg so the SAME assertions run on any host OS.
"""
from __future__ import annotations

import pytest

from systemu.runtime import platform_profile as pp

_OSES = ["win32", "darwin", "linux"]

_REQUIRED_KEYS = {
    "os", "os_family", "arch", "python_version", "capture_available",
    "keyring_backend", "forged_net_jail", "docker_mode", "provider_configured",
    "host_capabilities",
}
_ROW_KEYS = {"id", "label", "available", "via", "note"}
_HOST_CAP_IDS = {"record_capture", "com_uia", "hotkey", "host_browser"}


# ── AC-U6: identical schema across OS ────────────────────────────────────────

def test_schema_keys_identical_across_os():
    keysets = {p: set(pp.platform_profile(platform_str=p).keys()) for p in _OSES}
    ref = keysets["win32"]
    for p in _OSES:
        assert keysets[p] == ref, f"{p} schema drifted: {keysets[p] ^ ref}"
    assert _REQUIRED_KEYS <= ref


def test_host_capability_rows_have_stable_schema_across_os():
    for p in _OSES:
        rows = pp.platform_profile(platform_str=p)["host_capabilities"]
        assert rows and isinstance(rows, list)
        assert _HOST_CAP_IDS <= {r["id"] for r in rows}
        for r in rows:
            assert set(r.keys()) == _ROW_KEYS


def test_os_family_maps_each_os():
    fam = {p: pp.platform_profile(platform_str=p)["os_family"] for p in _OSES}
    assert fam == {"win32": "windows", "darwin": "macos", "linux": "linux"}


# ── AC-U6: the forged-network jail is ABSENT (IMPL-13 hard-DENY is why) ───────

def test_forged_net_jail_absent_on_every_os():
    for p in _OSES:
        assert pp.platform_profile(platform_str=p)["forged_net_jail"] == "absent"


# ── DEP-10: the container NEVER pretends a host capability is present ─────────

def test_container_defers_all_host_caps_to_host_companion():
    prof = pp.platform_profile(platform_str="linux", in_container=True)
    assert prof["docker_mode"] is True
    # no host desktop inside a container -> capture is not directly available
    assert prof["capture_available"] is False
    for r in prof["host_capabilities"]:
        assert r["available"] is False, f"{r['id']} must not claim present in a container"
        assert r["via"] == "host_companion"
        assert "Host Companion" in r["note"] and "flagged" in r["note"].lower()


def test_native_host_reports_capture_and_native_via():
    prof = pp.platform_profile(platform_str="win32", in_container=False)
    assert prof["docker_mode"] is False
    assert prof["capture_available"] is True
    rec = next(r for r in prof["host_capabilities"] if r["id"] == "record_capture")
    assert rec["available"] is True and rec["via"] == "native"


def test_com_uia_is_windows_only_on_a_native_host():
    win = next(r for r in pp.platform_profile(platform_str="win32", in_container=False)
               ["host_capabilities"] if r["id"] == "com_uia")
    lin = next(r for r in pp.platform_profile(platform_str="linux", in_container=False)
               ["host_capabilities"] if r["id"] == "com_uia")
    assert win["available"] is True
    assert lin["available"] is False   # honest: COM/UIA is a Windows-only capability


# ── keyring_backend: the OS→enum mapping (DEP-1/6) ───────────────────────────

def test_keyring_backend_maps_by_os_when_a_backend_is_usable():
    real = lambda: object()
    assert pp.keyring_backend("win32", usable=real) == "dpapi"
    assert pp.keyring_backend("darwin", usable=real) == "keychain"
    assert pp.keyring_backend("linux", usable=real) == "secretservice"


def test_keyring_backend_plaintext_fallback_when_no_backend_on_posix():
    none = lambda: None
    assert pp.keyring_backend("linux", usable=none) == "plaintext_fallback"
    assert pp.keyring_backend("darwin", usable=none) == "plaintext_fallback"


def test_keyring_backend_windows_dpapi_even_without_a_keyring():
    none = lambda: None
    assert pp.keyring_backend("win32", usable=none, dpapi=lambda: True) == "dpapi"
    assert pp.keyring_backend("win32", usable=none, dpapi=lambda: False) == "plaintext_fallback"


def test_profile_keyring_backend_is_a_valid_enum_on_every_os():
    valid = {"dpapi", "keychain", "secretservice", "plaintext_fallback"}
    for p in _OSES:
        assert pp.platform_profile(platform_str=p)["keyring_backend"] in valid


# ── provider_configured is driven by the environment (hermetic) ──────────────

def test_provider_configured_reflects_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-present")
    assert pp.platform_profile(platform_str="linux")["provider_configured"] is True
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert pp.platform_profile(platform_str="linux")["provider_configured"] is False


def test_arch_and_python_version_are_populated():
    prof = pp.platform_profile(platform_str="linux")
    assert isinstance(prof["arch"], str) and prof["arch"]
    assert prof["python_version"].count(".") >= 2
