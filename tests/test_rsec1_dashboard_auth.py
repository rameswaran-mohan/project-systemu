"""R-SEC1 — dashboard auth core (pure module; no UI, no dashboard wiring).

The NiceGUI dashboard ships with NO authentication. R-SEC1's rule:
  * loopback bind  -> auth OPTIONAL (frictionless default; warn only)
  * non-loopback   -> auth REQUIRED, else the dashboard REFUSES to start
    (fail-closed).

This suite pins the pure auth core: scrypt hashing (constant-time verify,
never-raise on garbage), the loopback classifier, the start-verdict matrix,
per-IP lockout, a persisted session secret, and the vault-backed passphrase
store. Stdlib only — no new dependency.
"""
from __future__ import annotations

import os

import pytest

from systemu.runtime import dashboard_auth as da


# --------------------------------------------------------------------------- #
# hashing + verify
# --------------------------------------------------------------------------- #

def test_hash_and_verify_roundtrip():
    h = da.hash_passphrase("correct horse")
    assert h.startswith("scrypt$")
    assert da.verify("correct horse", h) is True
    assert da.verify("wrong", h) is False


def test_verify_safe_on_garbage():
    assert da.verify("x", "") is False
    assert da.verify("x", "not-a-hash") is False          # MUST NOT raise
    # extra defensive shapes that must all fail-closed, never raise:
    assert da.verify("x", "scrypt$14$8$1$deadbeef") is False       # too few fields
    assert da.verify("x", "scrypt$14$8$1$nothex$nothex") is False  # bad hex
    assert da.verify("x", "bcrypt$14$8$1$aa$bb") is False          # wrong scheme
    assert da.verify("x", None) is False                           # type: ignore[arg-type]


def test_hash_is_salted_unique():
    assert da.hash_passphrase("pw") != da.hash_passphrase("pw")    # random salt


def test_hash_format_shape():
    h = da.hash_passphrase("pw")
    parts = h.split("$")
    assert parts[0] == "scrypt" and parts[1:4] == ["14", "8", "1"]
    assert len(parts) == 6
    bytes.fromhex(parts[4])   # salt hex parses
    bytes.fromhex(parts[5])   # dk hex parses
    assert len(bytes.fromhex(parts[4])) == 32   # 32-byte salt
    assert len(bytes.fromhex(parts[5])) == 32   # 32-byte derived key


# --------------------------------------------------------------------------- #
# loopback classifier
# --------------------------------------------------------------------------- #

def test_loopback_set():
    for h in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
        assert da.is_loopback(h) is True
    for h in ("0.0.0.0", "::", "", "192.168.1.5", "10.0.0.1"):
        assert da.is_loopback(h) is False


def test_loopback_is_case_and_space_insensitive():
    assert da.is_loopback("  LOCALHOST  ") is True
    assert da.is_loopback(" 127.0.0.1 ") is True
    assert da.is_loopback("not-a-host") is False


# --------------------------------------------------------------------------- #
# exposure / start verdict matrix
# --------------------------------------------------------------------------- #

def test_exposure_check_matrix():
    v = da.exposure_check("127.0.0.1", configured=False)
    assert v.may_start and v.warn and not v.require_auth
    v = da.exposure_check("localhost", configured=True)
    assert v.may_start and v.require_auth and not v.warn
    v = da.exposure_check("0.0.0.0", configured=False)
    assert not v.may_start and v.reason
    v = da.exposure_check("192.168.1.5", configured=True)
    assert v.may_start and v.require_auth


def test_exposure_reason_is_honest_and_actionable():
    v = da.exposure_check("0.0.0.0", configured=False)
    assert not v.may_start
    # names the host and points at both remedies
    assert "0.0.0.0" in v.reason
    assert "SYSTEMU_DASHBOARD_PASSPHRASE_HASH" in v.reason
    assert "127.0.0.1" in v.reason


def test_start_verdict_is_frozen():
    v = da.exposure_check("127.0.0.1", configured=False)
    with pytest.raises(Exception):
        v.may_start = False   # type: ignore[misc]  frozen dataclass


# --------------------------------------------------------------------------- #
# lockout
# --------------------------------------------------------------------------- #

def test_lockout_after_5(tmp_path):
    store = da.LockoutStore(tmp_path / "lock.json")
    ip = "1.2.3.4"
    for _ in range(5):
        assert not store.is_locked(ip)
        store.record_failure(ip)
    assert store.is_locked(ip)                    # 5th failure locks for 15 min


def test_lockout_success_clears(tmp_path):
    store = da.LockoutStore(tmp_path / "lock.json")
    ip = "9.9.9.9"
    for _ in range(3):
        store.record_failure(ip)
    store.record_success(ip)
    for _ in range(4):
        store.record_failure(ip)
    assert store.is_locked(ip) is False           # counter reset, not yet at 5


def test_lockout_is_per_ip(tmp_path):
    store = da.LockoutStore(tmp_path / "lock.json")
    for _ in range(5):
        store.record_failure("1.1.1.1")
    assert store.is_locked("1.1.1.1") is True
    assert store.is_locked("2.2.2.2") is False


def test_lockout_corrupt_file_is_defensive(tmp_path):
    p = tmp_path / "lock.json"
    p.write_text("{ broken")
    store = da.LockoutStore(p)
    assert store.is_locked("1.2.3.4") is False    # corrupt -> empty, never raises
    # and it can still record after a corrupt read without raising
    for _ in range(5):
        store.record_failure("1.2.3.4")
    assert store.is_locked("1.2.3.4") is True


def test_lockout_persists_across_instances(tmp_path):
    p = tmp_path / "lock.json"
    s1 = da.LockoutStore(p)
    for _ in range(5):
        s1.record_failure("7.7.7.7")
    s2 = da.LockoutStore(p)
    assert s2.is_locked("7.7.7.7") is True        # persisted to disk


# --------------------------------------------------------------------------- #
# session secret
# --------------------------------------------------------------------------- #

def test_session_secret_stable(tmp_path):
    s1 = da.session_secret(tmp_path)
    s2 = da.session_secret(tmp_path)
    assert s1 == s2 and len(s1) >= 32             # persisted + stable


def test_session_secret_differs_per_vault(tmp_path):
    a = da.session_secret(tmp_path / "a")
    b = da.session_secret(tmp_path / "b")
    assert a != b


# --------------------------------------------------------------------------- #
# vault-backed passphrase store
# --------------------------------------------------------------------------- #

def test_set_and_is_configured(tmp_path):
    assert da.is_configured_vault(tmp_path) is False
    da.set_passphrase(tmp_path, "hunter2")
    assert da.is_configured_vault(tmp_path) is True
    h = da.get_passphrase_hash_vault(tmp_path)
    assert da.verify("hunter2", h) is True


def test_get_hash_none_when_unset(tmp_path):
    assert da.get_passphrase_hash_vault(tmp_path) is None


def test_is_configured_env_overrides(tmp_path, monkeypatch):
    # env var alone is enough (config may expose it in a later task)
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    assert da.is_configured(None, tmp_path) is False
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", da.hash_passphrase("pw"))
    assert da.is_configured(None, tmp_path) is True


def test_is_configured_reads_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    assert da.is_configured(None, tmp_path) is False
    da.set_passphrase(tmp_path, "pw")
    assert da.is_configured(None, tmp_path) is True


def test_secret_file_perms_best_effort(tmp_path):
    # writing a passphrase must not raise even where chmod is a no-op (Windows).
    da.set_passphrase(tmp_path, "pw")
    # the secrets dir + file exist
    assert (tmp_path / "secrets" / "dashboard_auth.json").exists()


# --------------------------------------------------------------------------- #
# FINDING 1 — session secret must go through the S5 at-rest envelope, not
# plaintext. A local process/backup reading the vault dir must not get the raw
# session-signing key (which would let it forge dashboard sessions).
# --------------------------------------------------------------------------- #

def test_session_secret_not_plaintext_on_disk_when_encrypted(tmp_path):
    from systemu.runtime.credentials import at_rest

    s = da.session_secret(tmp_path)
    assert len(s) >= 32
    path = tmp_path / "secrets" / "dashboard_session.secret"
    assert path.exists()
    on_disk = path.read_text(encoding="utf-8")
    if at_rest.is_encrypted_at_rest():
        # the raw secret must NOT appear verbatim in the on-disk bytes
        assert s not in on_disk
    # stable round-trip through the envelope: a second call returns the SAME value
    assert da.session_secret(tmp_path) == s


# --------------------------------------------------------------------------- #
# FINDING 2 — a GLOBAL lockout counter (not only per-IP). A spray of one guess
# each from thousands of IPs must trip a global lock even though no single IP
# ever reaches the per-IP threshold.
# --------------------------------------------------------------------------- #

def test_global_lockout_on_spray_across_many_ips(tmp_path):
    store = da.LockoutStore(tmp_path / "lock.json")
    assert store.is_globally_locked() is False
    n = da.GLOBAL_FAILURE_THRESHOLD
    for i in range(n):
        store.record_failure(f"10.0.0.{i}")
    # global lock trips ...
    assert store.is_globally_locked() is True
    # ... yet NO single IP is per-IP locked (each had exactly one failure)
    assert store.is_locked("10.0.0.0") is False
    assert store.is_locked(f"10.0.0.{n - 1}") is False


# --------------------------------------------------------------------------- #
# FINDING 3 — the per-IP (and global) failure counter must reset after the
# lockout window expires, so each expiry starts a fresh N-strike window instead
# of collapsing the effective threshold to 1.
# --------------------------------------------------------------------------- #

def test_lockout_counter_resets_after_window_expiry(tmp_path, monkeypatch):
    store = da.LockoutStore(tmp_path / "lock.json")
    ip = "5.5.5.5"
    base = 1_000_000.0
    monkeypatch.setattr(da.time, "time", lambda: base)
    for _ in range(da.FAILURE_THRESHOLD):
        store.record_failure(ip)
    assert store.is_locked(ip) is True

    # jump past the lockout window -> no longer locked
    later = base + da.LOCKOUT_SECONDS + 1
    monkeypatch.setattr(da.time, "time", lambda: later)
    assert store.is_locked(ip) is False

    # a SINGLE post-expiry failure must NOT immediately re-lock (fresh window)
    store.record_failure(ip)
    assert store.is_locked(ip) is False


# --------------------------------------------------------------------------- #
# FINDING 4 — a corrupt passphrase file must fail CLOSED, not silently disable
# auth on a loopback bind. Distinguish "absent" (frictionless default) from
# "present-but-corrupt" (configured-but-broken -> require auth).
# --------------------------------------------------------------------------- #

def test_corrupt_passphrase_file_fails_closed(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)

    # genuinely ABSENT -> not configured (loopback frictionless default kept)
    assert da.is_configured(None, tmp_path) is False
    v = da.exposure_check("127.0.0.1", da.is_configured(None, tmp_path))
    assert v.require_auth is False

    # configure, then corrupt the file on disk
    da.set_passphrase(tmp_path, "hunter2")
    auth_file = tmp_path / "secrets" / "dashboard_auth.json"
    auth_file.write_text("{ broken", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        configured = da.is_configured(None, tmp_path)
    # present-but-corrupt -> stays configured (fail-closed), loud error logged
    assert configured is True
    assert any(r.levelno >= logging.ERROR for r in caplog.records)

    # a loopback bind must now REQUIRE auth, not the warn-only unauth verdict
    v = da.exposure_check("127.0.0.1", configured)
    assert v.require_auth is True
    assert v.warn is False

    # and nobody can actually log in until the operator fixes it
    assert da.get_passphrase_hash_vault(tmp_path) is None


# --------------------------------------------------------------------------- #
# FINDING A — the exposure gate and the login page must agree on WHAT hash to
# verify against. An env-only passphrase (the documented Docker path — set
# SYSTEMU_DASHBOARD_PASSPHRASE_HASH, no vault file) makes the guard ARM
# (is_configured True) but the login page resolved the hash ONLY via the vault
# (None env-only), so login was permanently impossible. get_active_passphrase_hash
# closes the divergence: env takes precedence, else the vault hash.
# --------------------------------------------------------------------------- #

def test_active_hash_env_only_docker_path(tmp_path, monkeypatch):
    """Env-only config (Docker path, NO vault file): the ACTIVE hash is the env
    hash, and an attempt with the matching passphrase VERIFIES end-to-end."""
    from systemu.interface.pages.login import _attempt_login

    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    env_hash = da.hash_passphrase("dockerpass")
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", env_hash)

    # NO vault file exists — vault dir has no secrets/dashboard_auth.json.
    assert da.get_passphrase_hash_vault(tmp_path) is None
    active = da.get_active_passphrase_hash(None, tmp_path)
    assert active == env_hash

    # the end-to-end Docker-path fix: login now verifies against the env hash.
    lockout = da.LockoutStore(tmp_path / "lock.json")
    ok, reason = _attempt_login("dockerpass", active, lockout, "203.0.113.5")
    assert ok is True
    assert reason == "ok"


def test_active_hash_env_takes_precedence_over_vault(tmp_path, monkeypatch):
    """When BOTH the env hash and a (different) vault hash are set, env wins."""
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    da.set_passphrase(tmp_path, "vault-pass")
    vault_hash = da.get_passphrase_hash_vault(tmp_path)
    assert vault_hash is not None

    env_hash = da.hash_passphrase("env-pass")
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", env_hash)

    active = da.get_active_passphrase_hash(None, tmp_path)
    assert active == env_hash
    assert active != vault_hash          # env precedence, not the vault value


def test_active_hash_vault_only(tmp_path, monkeypatch):
    """No env: the active hash falls back to the vault-stored hash."""
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    da.set_passphrase(tmp_path, "vault-only-pass")
    vault_hash = da.get_passphrase_hash_vault(tmp_path)
    assert da.get_active_passphrase_hash(None, tmp_path) == vault_hash


def test_active_hash_none_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    assert da.get_active_passphrase_hash(None, tmp_path) is None


def test_corrupt_file_active_hash_none_but_is_configured_stays_true(tmp_path, monkeypatch):
    """The intentional Finding-4 divergence: a present-but-corrupt vault file
    keeps is_configured True (fail-closed — guard stays armed) yet
    get_active_passphrase_hash is None (nobody can log in until repaired — the
    correct HARD fail-closed). Finding A must NOT reintroduce a fail-OPEN here."""
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    da.set_passphrase(tmp_path, "x")
    auth_file = tmp_path / "secrets" / "dashboard_auth.json"
    auth_file.write_text("{ broken", encoding="utf-8")

    # is_configured stays True (fail-closed, guard armed) — unchanged.
    assert da.is_configured(None, tmp_path) is True
    # but the active hash is None → login is impossible (correct hard-fail-closed).
    assert da.get_active_passphrase_hash(None, tmp_path) is None


# --------------------------------------------------------------------------- #
# capability_row — the deterministic auth/TLS posture the privacy/health page
# renders (COMPLIANCE-SPEC §CMP-0.a design 7 / UX-6). Pure: no side effects.
# --------------------------------------------------------------------------- #

# alias matching the task-snippet naming; `config` only passes through to
# is_configured (which reads the env + vault), so a bare None is sufficient.
dashboard_auth = da


def _cfg():
    return None


def test_capability_row_loopback_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    monkeypatch.delenv("SYSTEMU_TLS_CERT", raising=False)
    row = dashboard_auth.capability_row(_cfg(), tmp_path, "127.0.0.1")
    assert row["dashboard_auth"] == "none(loopback-only)"
    assert row["tls"] == "n/a(loopback)"


def test_capability_row_nonloopback_configured_tls(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", "scrypt$14$8$1$aa$bb")
    monkeypatch.setenv("SYSTEMU_TLS_CERT", "x"); monkeypatch.setenv("SYSTEMU_TLS_KEY", "y")
    row = dashboard_auth.capability_row(_cfg(), tmp_path, "0.0.0.0")
    assert row["dashboard_auth"] == "session"
    assert row["tls"] == "on"


def test_capability_row_nonloopback_no_tls(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", "scrypt$14$8$1$aa$bb")
    monkeypatch.delenv("SYSTEMU_TLS_CERT", raising=False)
    row = dashboard_auth.capability_row(_cfg(), tmp_path, "0.0.0.0")
    assert row["tls"] == "off"
