"""R-UX1 (SPEC §15-DEP DEP-1/6) — POSIX secrets route through the OS keyring;
the plaintext file is a FLAGGED last-resort only.

Security invariants proven here:
  * with a keyring backend available, a saved secret is NEVER written to the
    plaintext fallback file (AC-U6);
  * with NO backend, the flagged plaintext fallback is used and the profile
    reports ``keyring_backend=plaintext_fallback``;
  * a keyring ERROR falls back LOUDLY (a WARNING is logged) and never loses the
    secret silently — and the log NEVER contains the secret value.
"""
from __future__ import annotations

import logging

from systemu.runtime.credentials import at_rest
from systemu.runtime.credentials.store import CredentialStore

_SECRET = "topsecretvalue-abcdef"


class _FakeKeyring:
    """An in-memory OS keyring stand-in (hermetic — no real Credential Manager /
    Keychain writes)."""

    def __init__(self):
        self.store = {}

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        self.store.pop((service, key), None)


def test_secret_not_written_to_plaintext_when_keyring_available(tmp_path):
    cs = CredentialStore(base_dir=tmp_path)
    fake = _FakeKeyring()
    cs._keyring = fake

    backend = cs.set("API_KEY", _SECRET)

    assert backend == "keyring"
    assert fake.store[("systemu", "API_KEY")] == _SECRET     # the OS keyring holds it
    # THE INVARIANT: nothing readable landed in the plaintext fallback file.
    f = tmp_path / ".credentials.json"
    assert (not f.exists()) or (_SECRET not in f.read_text(encoding="utf-8"))
    assert cs.get("API_KEY") == _SECRET


def test_no_backend_uses_flagged_plaintext_fallback_and_reports_it(tmp_path):
    cs = CredentialStore(base_dir=tmp_path)
    cs._keyring = None                       # no OS keyring backend

    backend = cs.set("API_KEY", "filesecretvalue")

    assert backend == "file"
    assert cs.get("API_KEY") == "filesecretvalue"
    f = tmp_path / ".credentials.json"
    assert f.exists()
    # POSIX-secondary (no DPAPI): the file is genuinely plaintext — this proves
    # the fallback path is the plaintext-file one, not keyring.
    if not at_rest.is_encrypted_at_rest():
        assert "filesecretvalue" in f.read_text(encoding="utf-8")
    # …and the profile flags it as the plaintext fallback when no backend exists.
    from systemu.runtime import platform_profile as pp
    assert pp.keyring_backend("linux", usable=lambda: None) == "plaintext_fallback"


def test_keyring_error_falls_back_loudly_never_silently(tmp_path, caplog):
    class _BoomKeyring:
        def get_password(self, s, k):
            return None

        def set_password(self, s, k, v):
            raise RuntimeError("keyring is locked")

        def delete_password(self, s, k):
            pass

    cs = CredentialStore(base_dir=tmp_path)
    cs._keyring = _BoomKeyring()

    with caplog.at_level(logging.WARNING, logger="systemu.runtime.credentials.store"):
        backend = cs.set("API_KEY", "loudsecret")

    # the secret was preserved via the fallback, not lost
    assert backend == "file"
    assert cs.get("API_KEY") == "loudsecret"
    # LOUD: a WARNING was emitted about the keyring
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("keyring" in r.getMessage().lower() for r in warnings), \
        "a keyring failure must fall back LOUDLY, not silently"
    # …and no log record leaked the secret VALUE
    assert all("loudsecret" not in r.getMessage() for r in caplog.records)


def test_usable_keyring_rejects_the_fail_and_null_sentinel_backends(monkeypatch):
    import keyring
    from keyring.backends import fail, null
    from systemu.runtime.credentials import store as st

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    assert st.usable_keyring() is None            # a headless box: no real backend

    monkeypatch.setattr(keyring, "get_keyring", lambda: null.Keyring())
    assert st.usable_keyring() is None


def test_usable_keyring_contract_never_raises():
    # On any host it returns either a keyring-like object (has get_password) or
    # None — and never raises (import-guarded).
    from systemu.runtime.credentials import store as st
    result = st.usable_keyring()
    assert result is None or hasattr(result, "get_password")


def test_a_healthy_keyring_backend_is_accepted_and_used():
    # A real backend (priority>0, not fail/null) must be honored so secrets go to
    # the OS keyring, not plaintext.
    import keyring
    from systemu.runtime.credentials import store as st

    class _Backend:
        priority = 5

        def get_password(self, s, k):
            return None

    class _Mod:
        @staticmethod
        def get_keyring():
            return _Backend()

    # usable_keyring returns the keyring module when the backend is viable
    import types
    orig = keyring.get_keyring
    try:
        keyring.get_keyring = _Mod.get_keyring
        assert st.usable_keyring() is keyring
    finally:
        keyring.get_keyring = orig
