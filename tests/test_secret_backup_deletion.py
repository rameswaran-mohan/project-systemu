"""S5 / R-A3 -- a plaintext backup is deleted after a VERIFIED re-encryption.

THE RULE
    The daemon-start pass copies a plaintext secret to ``<file>.pre-encrypt.bak``
    before encrypting it. Once the encrypted file has been read back from disk
    and decrypts to exactly the original bytes, a backup holding EXACTLY those
    bytes is only a plaintext copy of a secret that is now safe on disk, so it
    is deleted. Never deleted:
      * a backup whose bytes differ (an earlier copy the envelope cannot
        restore) -- tests/test_secrets_envelope_truth.py also pins this;
      * any backup when the verification failed -- the plaintext file stays,
        the backup stays, and the doctor keeps naming it.

HERMETIC: the DPAPI seam is faked; every vault is a ``tmp_path``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from systemu.runtime import platform_profile as pp
from systemu.runtime.credentials import at_rest

SECRET = "sk-backup-deletion-secret-0123456789abcdef"
_LOG = "systemu.runtime.credentials.at_rest"


class _FakeDPAPI:
    @staticmethod
    def CryptProtectData(data, desc, *rest):
        return bytes(b ^ 0x5A for b in data)

    @staticmethod
    def CryptUnprotectData(blob, *rest):
        return ("systemu-secret", bytes(b ^ 0x5A for b in blob))


class _CorruptingDPAPI(_FakeDPAPI):
    """Fools the envelope probe; a real secret decrypts to a different value."""

    @staticmethod
    def CryptUnprotectData(blob, *rest):
        clear = bytes(b ^ 0x5A for b in blob)
        return ("systemu-secret", clear.replace(SECRET.encode(), b"sk-corrupted-value"))


@pytest.fixture
def seam(monkeypatch):
    def _set(module):
        monkeypatch.setattr(at_rest, "_win32crypt", lambda: module)
    return _set


def _plant(path: Path, obj) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _backup_of(path: Path) -> Path:
    return path.with_name(path.name + at_rest.BACKUP_SUFFIX)


def _backup_rows(vault: Path):
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True,
                               platform_str="linux", in_container=False,
                               vault_dir=str(vault))
    return [p for p in r["problems"] if p["id"].startswith("secret_plaintext_backup:")]


def test_a_verified_success_deletes_every_backup_it_wrote(tmp_path, seam, caplog):
    seam(_FakeDPAPI)
    files = [tmp_path / ".credentials.json",
             tmp_path / "secrets" / "api_token.json",
             tmp_path / "connections" / "mcp_oauth" / "srv.json"]
    for f in files:
        _plant(f, {"value": SECRET})

    with caplog.at_level(logging.INFO, logger=_LOG):
        report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    rels = tuple(sorted(f.relative_to(tmp_path).as_posix() for f in files))
    assert tuple(sorted(report.reencrypted)) == rels
    assert tuple(sorted(report.backups_removed)) == tuple(
        sorted(r + at_rest.BACKUP_SUFFIX for r in rels))
    assert report.backups_removed == report.backed_up
    assert not list(tmp_path.rglob("*" + at_rest.BACKUP_SUFFIX))
    assert _backup_rows(tmp_path) == []
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "deleted once the encrypted file verified" in infos[0]
    assert SECRET not in caplog.text


def test_an_identical_backup_from_an_earlier_failed_start_is_deleted_once_it_verifies(tmp_path, seam):
    seam(_CorruptingDPAPI)
    f = tmp_path / "secrets" / "dashboard_auth.json"
    original = _plant(f, {"hash": SECRET})
    failed = at_rest.reencrypt_plaintext_secrets(tmp_path)
    assert failed.unverified and _backup_of(f).read_bytes() == original

    seam(_FakeDPAPI)
    report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    assert report.reencrypted == ("secrets/dashboard_auth.json",)
    assert report.backed_up == ()                       # it already existed
    assert report.backups_removed == ("secrets/dashboard_auth.json" + at_rest.BACKUP_SUFFIX,)
    assert not _backup_of(f).exists()
    assert _backup_rows(tmp_path) == []


def test_a_backup_whose_bytes_differ_is_never_deleted(tmp_path, seam):
    seam(_FakeDPAPI)
    f = tmp_path / ".credentials.json"
    _plant(f, {"k": SECRET})
    earlier = b'{"k": "an earlier secret the envelope cannot restore"}'
    _backup_of(f).write_bytes(earlier)

    report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    assert report.reencrypted == (".credentials.json",)
    assert report.backups_removed == ()
    assert _backup_of(f).read_bytes() == earlier
    rows = _backup_rows(tmp_path)
    assert len(rows) == 1 and "is encrypted" in rows[0]["message"]


def test_a_failed_verification_keeps_the_backup_and_the_warning(tmp_path, seam):
    seam(_CorruptingDPAPI)
    f = tmp_path / ".credentials.json"
    original = _plant(f, {"k": SECRET})

    report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    assert report.reencrypted == () and report.backups_removed == ()
    assert f.read_bytes() == original
    assert _backup_of(f).read_bytes() == original
    rows = _backup_rows(tmp_path)
    assert len(rows) == 1 and "still plaintext" in rows[0]["message"]


def test_the_daemon_start_hook_leaves_no_plaintext_copy(tmp_path, seam, monkeypatch):
    from systemu.scheduler import daemon as daemon_mod
    seam(_FakeDPAPI)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
    _plant(tmp_path / "secrets" / "api_token.json", {"token_hash": SECRET})

    daemon_mod._reencrypt_plaintext_secrets_at_start()

    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()
                 and SECRET.encode() in p.read_bytes()]
    assert leftovers == [], f"plaintext copies of the secret remain: {leftovers}"
