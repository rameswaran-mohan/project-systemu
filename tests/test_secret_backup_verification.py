"""S5 / R-A3 -- re-encryption is VERIFIED before it replaces a plaintext secret,
and a plaintext backup left on disk is never silent.

THE DEFECT
    The daemon-start pass (2d) wrote ``<file>.pre-encrypt.bak`` -- a plaintext
    copy of the secret -- and swapped the envelope in after checking only an
    IN-MEMORY round trip of a RE-SERIALISED value: nothing read the encrypted
    file back from disk, and nothing compared it with the original BYTES. The
    backups were kept forever (0600 is a no-op on Windows), nothing told the
    operator they were there, and ``.gitignore`` did not cover them.

THE RULING (this slice)
    * the envelope seals the original text exactly; the encrypted file, read
      back from disk, must decrypt to exactly the original bytes BEFORE it
      replaces the plaintext file;
    * on failure the plaintext file stays in place untouched, the backup is
      kept, and a doctor warning names the backup path and what to do;
    * ``*.pre-encrypt.bak`` is git-ignored.

    (Deleting the backup after a VERIFIED success is the rest of the ruling; it
    is held: append-only pins in tests/test_secrets_envelope_truth.py assert the
    backup still exists after a successful pass.)

HERMETIC: the DPAPI seam is faked; every vault is a ``tmp_path``; the doctor is
always handed that vault. Nothing touches the real OS secret store or home.
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from systemu.runtime import platform_profile as pp
from systemu.runtime.credentials import at_rest

REPO = Path(__file__).resolve().parents[1]
SECRET = "sk-backup-verification-secret-0123456789abcdef"
_LOG = "systemu.runtime.credentials.at_rest"


class _FakeDPAPI:
    """A stand-in ``win32crypt`` whose ciphertext never contains the plaintext."""

    @staticmethod
    def CryptProtectData(data, desc, *rest):
        return bytes(b ^ 0x5A for b in data)

    @staticmethod
    def CryptUnprotectData(blob, *rest):
        return ("systemu-secret", bytes(b ^ 0x5A for b in blob))


class _CorruptingDPAPI(_FakeDPAPI):
    """Round-trips the envelope PROBE (so the envelope reads as working), but a
    real secret decrypts to a DIFFERENT value -- still valid JSON, so only a
    comparison with the original bytes can tell."""

    @staticmethod
    def CryptUnprotectData(blob, *rest):
        clear = bytes(b ^ 0x5A for b in blob)
        return ("systemu-secret", clear.replace(SECRET.encode(), b"sk-corrupted-value"))


@pytest.fixture
def seam(monkeypatch):
    def _set(module):
        monkeypatch.setattr(at_rest, "_win32crypt", lambda: module)
    return _set


def _plant(path: Path, raw: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _decrypt_on_disk(path: Path) -> bytes:
    outer = json.loads(path.read_text(encoding="utf-8"))
    assert outer[at_rest._ENC_MARKER] == "dpapi"
    return _FakeDPAPI.CryptUnprotectData(base64.b64decode(outer["data"]))[1]


def _backup_of(path: Path) -> Path:
    return path.with_name(path.name + at_rest.BACKUP_SUFFIX)


def _doctor(vault: Path):
    return pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                                  keyring_locked=False, daemon_running=True,
                                  platform_str="linux", in_container=False,
                                  vault_dir=str(vault))


def _backup_rows(report):
    return [p for p in report["problems"] if p["id"].startswith("secret_plaintext_backup:")]


# --------------------------------------------------------------------------- #
# the envelope seals the ORIGINAL bytes, verified from disk
# --------------------------------------------------------------------------- #

def test_the_encrypted_file_decrypts_to_exactly_the_original_bytes(tmp_path, seam):
    seam(_FakeDPAPI)
    f = tmp_path / ".credentials.json"
    # Pretty-printed: re-serialising the VALUE would not give these bytes back.
    original = _plant(f, json.dumps({"openai_api_key": SECRET}, indent=2).encode("utf-8"))

    report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    assert report.reencrypted == (".credentials.json",) and report.unverified == ()
    assert SECRET not in f.read_text(encoding="utf-8")
    assert _decrypt_on_disk(f) == original
    # ...and every reader still gets the same value through the one seam.
    assert at_rest.unprotect_json(f.read_text(encoding="utf-8")) == {"openai_api_key": SECRET}
    assert not list(tmp_path.rglob("*.reencrypt.tmp"))


# --------------------------------------------------------------------------- #
# a corrupting envelope: the plaintext stays, the backup stays, it is reported
# --------------------------------------------------------------------------- #

def test_a_corrupting_envelope_never_replaces_the_plaintext_file(tmp_path, seam, caplog):
    seam(_CorruptingDPAPI)
    assert at_rest.envelope_status().working is True     # the probe alone is fooled
    f = tmp_path / ".credentials.json"
    original = _plant(f, json.dumps({"openai_api_key": SECRET}).encode("utf-8"))

    with caplog.at_level(logging.INFO, logger=_LOG):
        report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    assert f.read_bytes() == original, "an unverified envelope replaced the plaintext file"
    backup = _backup_of(f)
    assert backup.read_bytes() == original, "the backup must be kept on failure"
    assert report.reencrypted == ()
    assert len(report.unverified) == 1 and report.unverified[0].startswith(".credentials.json: ")
    assert not list(tmp_path.rglob("*.reencrypt.tmp"))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(backup) in w and "NOT re-encrypted" in w for w in warnings), warnings
    assert SECRET not in caplog.text


def test_a_second_start_with_the_same_fault_is_idempotent(tmp_path, seam):
    seam(_CorruptingDPAPI)
    f = tmp_path / "secrets" / "api_token.json"
    original = _plant(f, json.dumps({"token_hash": SECRET}).encode("utf-8"))
    at_rest.reencrypt_plaintext_secrets(tmp_path)
    backup = _backup_of(f)
    backup.write_bytes(b"the first start's copy")      # a rewrite would clobber this

    report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    assert f.read_bytes() == original
    assert backup.read_bytes() == b"the first start's copy"
    assert report.backed_up == () and report.reencrypted == ()
    assert len(report.unverified) == 1
    assert sorted(p.name for p in f.parent.iterdir()) == sorted(
        [f.name, backup.name])


def test_once_the_envelope_is_fixed_the_next_start_encrypts_it(tmp_path, seam):
    seam(_CorruptingDPAPI)
    f = tmp_path / "connections" / "mcp_oauth" / "srv.json"
    original = _plant(f, json.dumps({"access_token": SECRET}).encode("utf-8"))
    at_rest.reencrypt_plaintext_secrets(tmp_path)
    seam(_FakeDPAPI)

    report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    assert report.reencrypted == ("connections/mcp_oauth/srv.json",)
    assert report.unverified == () and report.backed_up == ()
    assert _decrypt_on_disk(f) == original


# --------------------------------------------------------------------------- #
# the doctor names every plaintext backup and what to do
# --------------------------------------------------------------------------- #

def test_doctor_names_the_kept_backup_of_a_file_that_stayed_plaintext(tmp_path, seam):
    seam(_CorruptingDPAPI)
    f = tmp_path / ".credentials.json"
    _plant(f, json.dumps({"k": SECRET}).encode("utf-8"))
    at_rest.reencrypt_plaintext_secrets(tmp_path)

    rows = _backup_rows(_doctor(tmp_path))
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "warning" and row["blocking"] is False
    assert str(_backup_of(f)) in row["message"] and str(f) in row["message"]
    assert "still plaintext" in row["message"]
    assert "restart the daemon" in row["cta"].lower() and "delete" in row["cta"].lower()
    assert row["message"].isascii() and row["cta"].isascii()
    assert SECRET not in json.dumps(rows)


def test_doctor_names_a_backup_left_next_to_an_encrypted_file(tmp_path, seam):
    seam(_FakeDPAPI)
    f = tmp_path / "secrets" / "dashboard_auth.json"
    _plant(f, json.dumps({"hash": SECRET}).encode("utf-8"))
    # An earlier copy with DIFFERENT bytes: the envelope cannot restore it, so a
    # verified success keeps it -- the state this doctor row is about.
    _backup_of(f).write_bytes(b'{"hash": "an earlier copy"}')
    at_rest.reencrypt_plaintext_secrets(tmp_path)
    assert _backup_of(f).is_file()        # a backup whose bytes differ is kept

    rows = _backup_rows(_doctor(tmp_path))
    assert len(rows) == 1
    assert str(_backup_of(f)) in rows[0]["message"]
    assert "is encrypted" in rows[0]["message"]
    assert str(_backup_of(f)) in rows[0]["cta"] and "delete" in rows[0]["cta"].lower()


def test_doctor_is_quiet_when_no_backup_exists(tmp_path, seam):
    seam(_FakeDPAPI)
    _plant(tmp_path / "secrets" / "api_token.json", b'{"x": 1}')
    assert _backup_rows(_doctor(tmp_path)) == []


def test_doctor_scans_the_vault_the_daemon_reencrypts_by_default(tmp_path, monkeypatch, seam):
    """No ``vault_dir``: the scan resolves the SAME root as the daemon-start pass
    (``resolve_vault_root``), so the warning reaches `doctor` and /health."""
    seam(_CorruptingDPAPI)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
    f = tmp_path / ".credentials.json"
    _plant(f, json.dumps({"k": SECRET}).encode("utf-8"))
    from systemu.scheduler import daemon as daemon_mod
    daemon_mod._reencrypt_plaintext_secrets_at_start()

    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True,
                               platform_str="linux", in_container=False)
    assert [str(_backup_of(f)) in p["message"] for p in _backup_rows(r)] == [True]


def test_an_injected_backup_list_is_used_verbatim():
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True,
                               platform_str="linux", in_container=False,
                               secret_backups=[])
    assert _backup_rows(r) == []


# --------------------------------------------------------------------------- #
# .gitignore
# --------------------------------------------------------------------------- #

def test_plaintext_backups_are_git_ignored():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*" + at_rest.BACKUP_SUFFIX in [ln.strip() for ln in lines]
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not on PATH")
    for rel in (".credentials.json.pre-encrypt.bak",
                "systemu/vault/secrets/api_token.json.pre-encrypt.bak",
                "some/vault/connections/mcp_oauth/srv.json.pre-encrypt.bak"):
        res = subprocess.run([git, "check-ignore", "--no-index", "-q", rel],
                             cwd=str(REPO), capture_output=True)
        assert res.returncode == 0, f"{rel} is not git-ignored"
