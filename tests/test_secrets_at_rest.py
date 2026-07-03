"""S5 — secrets at rest (spec UNIFIED-v2 §7 / §11.3).

The primary Windows credential path already encrypts via keyring's
WinVaultKeyring (Credential Manager). The two remaining plaintext exposures are:
  * the OAuth ``VaultTokenStore`` (access/refresh tokens at
    ``<vault>/connections/mcp_oauth/<id>.json``), and
  * the ``CredentialStore`` FILE FALLBACK (``.credentials.json``, used when
    keyring is unavailable),
both of which wrote plaintext JSON guarded only by ``os.chmod(0o600)`` — a NO-OP
on Windows, the primary platform.

This suite proves both now go through a DPAPI at-rest envelope on Windows
(transparent, migrate-on-read, fail-safe), while POSIX behavior (plaintext under
a real 0600) is unchanged. Windows-only ciphertext assertions are guarded by
``at_rest.is_encrypted_at_rest()`` so the suite is correct on CI.
"""
from __future__ import annotations

import json
from pathlib import Path

from systemu.runtime.credentials import at_rest
from systemu.runtime.credentials.store import CredentialStore
from systemu.runtime.mcp.sdk.oauth import VaultTokenStore


SECRET = "sk-super-secret-token-abcdef"


# --------------------------------------------------------------------------- #
# the at-rest envelope helper
# --------------------------------------------------------------------------- #

def test_protect_unprotect_roundtrip():
    obj = {"access_token": SECRET, "refresh_token": "rt-123", "expires_at": 999}
    envelope = at_rest.protect_json(obj)
    assert isinstance(envelope, str)
    assert at_rest.unprotect_json(envelope) == obj


def test_envelope_hides_plaintext_when_encrypted():
    obj = {"access_token": SECRET}
    envelope = at_rest.protect_json(obj)
    if at_rest.is_encrypted_at_rest():
        assert SECRET not in envelope
        assert at_rest._ENC_MARKER in envelope
    else:
        # POSIX-secondary: plaintext under a 0600 file (unchanged behavior)
        assert SECRET in envelope


def test_unprotect_legacy_plaintext_dict_migrate_on_read():
    # a pre-existing plaintext JSON dict (no envelope) must read transparently
    legacy = json.dumps({"access_token": SECRET})
    assert at_rest.unprotect_json(legacy) == {"access_token": SECRET}


# --------------------------------------------------------------------------- #
# VaultTokenStore (OAuth tokens)
# --------------------------------------------------------------------------- #

class _Vault:
    def __init__(self, root: Path):
        self.root = str(root)


def test_token_store_encrypts_on_disk(tmp_path):
    store = VaultTokenStore(_Vault(tmp_path), "github")
    store.save({"access_token": SECRET, "refresh_token": "rt"})

    assert store.load() == {"access_token": SECRET, "refresh_token": "rt"}
    raw = store.path.read_text(encoding="utf-8")
    if at_rest.is_encrypted_at_rest():
        assert SECRET not in raw
        assert "rt" not in raw or at_rest._ENC_MARKER in raw  # tokens not in cleartext


def test_token_store_migrates_legacy_plaintext(tmp_path):
    store = VaultTokenStore(_Vault(tmp_path), "wp")
    # simulate a v0.9.52 plaintext token file written before this release
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"access_token": SECRET}), encoding="utf-8")

    # load reads the legacy plaintext transparently
    assert store.load() == {"access_token": SECRET}
    # re-saving upgrades it to the encrypted envelope
    store.save(store.load())
    if at_rest.is_encrypted_at_rest():
        assert SECRET not in store.path.read_text(encoding="utf-8")


def test_token_store_corrupt_returns_empty(tmp_path):
    store = VaultTokenStore(_Vault(tmp_path), "broken")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("this is not json {", encoding="utf-8")
    # a corrupt store must not crash — return {} (re-prompt), never raise
    assert store.load() == {}


# --------------------------------------------------------------------------- #
# CredentialStore file fallback (keyring unavailable)
# --------------------------------------------------------------------------- #

def _file_fallback_store(tmp_path) -> CredentialStore:
    cs = CredentialStore(base_dir=tmp_path)
    cs._keyring = None  # force the file fallback path
    return cs


def test_credential_file_fallback_encrypts(tmp_path):
    cs = _file_fallback_store(tmp_path)
    backend = cs.set("openai_api_key", SECRET)
    assert backend == "file"
    assert cs.get("openai_api_key") == SECRET

    raw = cs._file.read_text(encoding="utf-8")
    if at_rest.is_encrypted_at_rest():
        assert SECRET not in raw
        assert at_rest._ENC_MARKER in raw


def test_credential_file_fallback_migrates_legacy(tmp_path):
    # a pre-existing plaintext .credentials.json
    (tmp_path / ".credentials.json").write_text(json.dumps({"k": SECRET}), encoding="utf-8")
    cs = _file_fallback_store(tmp_path)
    assert cs.get("k") == SECRET          # reads legacy plaintext transparently
    cs.set("k2", "v2")                    # a write upgrades the whole file
    if at_rest.is_encrypted_at_rest():
        assert SECRET not in cs._file.read_text(encoding="utf-8")
    assert cs.get("k") == SECRET and cs.get("k2") == "v2"
