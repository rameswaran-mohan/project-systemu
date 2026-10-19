"""2d -- S5 truthfulness: the profile may say ``dpapi`` only when DPAPI WORKS.

THE DEFECT
    * ``pywin32`` (which provides ``win32crypt``, i.e. DPAPI) was never a declared
      dependency. It reached Windows installs only because ``mcp`` happened to
      pull it in, and nothing guarantees that.
    * Without it the at-rest envelope silently degrades to plaintext JSON, while
      the profile's DPAPI claim was an IMPORT check (or, with a usable keyring,
      no check of the envelope at all).
    * Plaintext secrets written before the envelope existed were re-encrypted
      only on their next write, with no backup.

THE RULING
    (a) declare ``pywin32>=306; sys_platform == "win32"`` in the core deps;
    (b) the profile's secrets row is derived from a WORKING envelope -- a real
        protect/unprotect round trip through the same seam real secrets use --
        and reads ``dpapi``, ``keyring`` or ``plaintext (<reason>)``;
    (c) at daemon start every plaintext secret file is re-encrypted after a
        one-time ``<file>.pre-encrypt.bak`` (0600 where the OS has modes), logged
        once, idempotent.

HERMETIC: the DPAPI seam (``at_rest._win32crypt``) and the keyring probe are
faked in every test here. Nothing touches the real OS secret store or home;
every vault is a ``tmp_path``.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from systemu.runtime import platform_profile as pp
from systemu.runtime.credentials import at_rest

REPO = Path(__file__).resolve().parents[1]
SECRET = "sk-envelope-truth-secret-value-0123456789"


class _FakeDPAPI:
    """A stand-in ``win32crypt`` whose ciphertext never contains the plaintext."""

    @staticmethod
    def CryptProtectData(data, desc, *rest):
        return bytes(b ^ 0x5A for b in data)

    @staticmethod
    def CryptUnprotectData(blob, *rest):
        return ("systemu-secret", bytes(b ^ 0x5A for b in blob))


class _BrokenDPAPI:
    """Importable, but every protect call fails (a DPAPI that does not work)."""

    @staticmethod
    def CryptProtectData(data, desc, *rest):
        raise OSError("DPAPI refused")

    @staticmethod
    def CryptUnprotectData(blob, *rest):
        raise OSError("DPAPI refused")


class _ClearDPAPI:
    """Importable and 'succeeds' -- but the blob IS the plaintext."""

    @staticmethod
    def CryptProtectData(data, desc, *rest):
        return bytes(data)

    @staticmethod
    def CryptUnprotectData(blob, *rest):
        return ("systemu-secret", bytes(blob))


@pytest.fixture
def seam(monkeypatch):
    """Install a fake DPAPI seam: ``seam(None)`` = pywin32 absent."""
    def _set(module):
        monkeypatch.setattr(at_rest, "_win32crypt", lambda: module)
    return _set


@pytest.fixture
def no_keyring(monkeypatch):
    monkeypatch.setattr(pp, "_usable_keyring", lambda: None)


@pytest.fixture
def a_keyring(monkeypatch):
    monkeypatch.setattr(pp, "_usable_keyring", lambda: object())


# --------------------------------------------------------------------------- #
# (a) pywin32 is a declared core dependency on Windows, and only there
# --------------------------------------------------------------------------- #

def test_pywin32_is_a_declared_windows_only_core_dependency():
    try:
        import tomllib
    except ImportError:  # Python 3.10: the [dev] extra ships tomli
        import tomli as tomllib
    from packaging.requirements import Requirement

    deps = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["dependencies"]
    reqs = [Requirement(d) for d in deps if Requirement(d).name.lower() == "pywin32"]
    assert len(reqs) == 1, f"pywin32 must be declared exactly once in core deps: {reqs}"
    req = reqs[0]
    assert req.specifier.contains("306") and not req.specifier.contains("305"), str(req)
    assert req.marker is not None
    assert req.marker.evaluate({"sys_platform": "win32"}) is True
    for other in ("linux", "darwin"):
        assert req.marker.evaluate({"sys_platform": other}) is False, other


# --------------------------------------------------------------------------- #
# (b) the envelope probe: a real round trip through the seam
# --------------------------------------------------------------------------- #

def test_the_envelope_works_only_through_a_working_round_trip(seam):
    seam(_FakeDPAPI)
    status = at_rest.envelope_status()
    assert status.working is True and status.reason == ""
    assert at_rest.is_encrypted_at_rest() is True


def test_an_absent_seam_is_not_a_working_envelope_and_says_why(seam):
    seam(None)
    status = at_rest.envelope_status()
    assert status.working is False
    assert "pywin32" in status.reason
    assert at_rest.is_encrypted_at_rest() is False


def test_an_importable_but_failing_dpapi_is_not_a_working_envelope(seam):
    # The old probe was `import win32crypt` -- this case read as encrypted.
    seam(_BrokenDPAPI)
    status = at_rest.envelope_status()
    assert status.working is False
    assert "OSError" in status.reason and status.reason.isascii()
    assert at_rest.is_encrypted_at_rest() is False


def test_an_envelope_that_leaves_the_value_readable_is_not_working(seam):
    seam(_ClearDPAPI)
    status = at_rest.envelope_status()
    assert status.working is False
    assert "readable" in status.reason


# --------------------------------------------------------------------------- #
# (b) the profile's secrets row
# --------------------------------------------------------------------------- #

def test_seam_unavailable_profile_says_plaintext_with_the_reason_never_dpapi(seam, no_keyring):
    seam(None)
    row = pp.platform_profile(platform_str="win32", provider_configured=True)["secrets_at_rest"]
    assert row["backend"] == "plaintext"
    assert row["label"].startswith("plaintext (") and row["label"].endswith(")")
    assert "pywin32" in row["reason"] and row["reason"] in row["label"]
    assert "dpapi" not in row["backend"]
    assert row["label"].isascii()


def test_seam_available_profile_says_dpapi(seam, no_keyring):
    seam(_FakeDPAPI)
    row = pp.platform_profile(platform_str="win32", provider_configured=True)["secrets_at_rest"]
    assert row == {"backend": "dpapi", "label": "dpapi", "reason": ""}


def test_a_broken_dpapi_never_earns_the_dpapi_claim(seam, no_keyring):
    seam(_BrokenDPAPI)
    row = pp.secrets_at_rest("win32")
    assert row["backend"] == "plaintext" and "OSError" in row["reason"]
    # ...and the keyring row's DPAPI probe is the same working round trip.
    assert pp.keyring_backend("win32", usable=lambda: None) == "plaintext_fallback"


def test_no_envelope_but_a_keyring_reports_keyring_and_names_what_stays_plaintext(seam, a_keyring):
    seam(None)
    row = pp.secrets_at_rest("win32")
    assert row["backend"] == "keyring" and row["label"] == "keyring"
    assert "pywin32" in row["reason"] and "plaintext" in row["reason"]


def test_posix_rows_never_claim_dpapi(seam, a_keyring, monkeypatch):
    seam(_FakeDPAPI)       # even a working seam cannot make POSIX "dpapi"
    for p in ("linux", "darwin"):
        assert pp.secrets_at_rest(p)["backend"] == "keyring"
    monkeypatch.setattr(pp, "_usable_keyring", lambda: None)
    for p in ("linux", "darwin"):
        row = pp.secrets_at_rest(p)
        assert row["backend"] == "plaintext" and row["label"].startswith("plaintext (")


def test_secrets_row_schema_is_identical_across_os(seam, no_keyring):
    seam(None)
    keys = {p: set(pp.platform_profile(platform_str=p, provider_configured=True)
                   ["secrets_at_rest"]) for p in ("win32", "darwin", "linux")}
    assert keys["win32"] == keys["darwin"] == keys["linux"] == {"backend", "label", "reason"}


def test_doctor_warns_when_windows_secrets_files_are_plaintext(seam, a_keyring):
    seam(None)
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True,
                               platform_str="win32", in_container=False)
    rows = [p for p in r["problems"] if p["id"] == "secrets_envelope_unavailable"]
    assert len(rows) == 1 and rows[0]["blocking"] is False
    assert "pywin32" in rows[0]["message"]
    assert rows[0]["message"].isascii() and rows[0]["cta"].isascii()


def test_doctor_is_quiet_about_the_envelope_when_it_works(seam, a_keyring):
    seam(_FakeDPAPI)
    r = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                               keyring_locked=False, daemon_running=True,
                               platform_str="win32", in_container=False)
    assert not [p for p in r["problems"] if p["id"] == "secrets_envelope_unavailable"]


# --------------------------------------------------------------------------- #
# (c) re-encrypt plaintext secrets at daemon start, after a one-time backup
# --------------------------------------------------------------------------- #

def _plant(path: Path, obj) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _assert_envelope_of(path: Path, obj):
    text = path.read_text(encoding="utf-8")
    assert at_rest._ENC_MARKER in text and SECRET not in text, text[:200]
    assert at_rest.unprotect_json(text) == obj


def test_a_plaintext_secret_is_reencrypted_after_a_one_time_backup(tmp_path, seam, caplog,
                                                                  monkeypatch):
    # GATE-6 (S5 / R-A3): the backup is a plaintext secret, so a VERIFIED
    # success deletes it. What it held and how it was created are witnessed at
    # the moment it is written; after the pass it must be gone.
    seam(_FakeDPAPI)
    f = tmp_path / ".credentials.json"
    original = _plant(f, {"openai_api_key": SECRET})
    written = []
    real_write = at_rest._write_new_0600

    def _witness_backup(path, data):
        real_write(path, data)
        written.append((path, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)))

    monkeypatch.setattr(at_rest, "_write_new_0600", _witness_backup)

    with caplog.at_level(logging.INFO, logger="systemu.runtime.credentials.at_rest"):
        report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    _assert_envelope_of(f, {"openai_api_key": SECRET})
    backup = tmp_path / (".credentials.json" + at_rest.BACKUP_SUFFIX)
    assert backup.name == ".credentials.json.pre-encrypt.bak"
    assert [(p, b) for p, b, _m in written] == [(backup, original)]
    assert not backup.exists(), "a verified success must delete the plaintext backup"
    if os.name == "posix":
        assert written[0][2] == 0o600
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert report.reencrypted == (".credentials.json",)
    assert report.backed_up == (".credentials.json.pre-encrypt.bak",)
    assert report.backups_removed == (".credentials.json.pre-encrypt.bak",)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1, [r.getMessage() for r in infos]
    assert SECRET not in caplog.text


def test_a_second_start_writes_no_new_backup_and_logs_nothing(tmp_path, seam, caplog):
    # GATE-6 (S5 / R-A3): the first start's backup is gone after its verified
    # success, so "a second start wrote a new backup" is simply "one exists".
    seam(_FakeDPAPI)
    f = tmp_path / ".credentials.json"
    _plant(f, {"k": SECRET})
    first = at_rest.reencrypt_plaintext_secrets(tmp_path)
    backup = tmp_path / (".credentials.json" + at_rest.BACKUP_SUFFIX)
    assert not backup.exists(), "the first start's verified success kept the backup"
    encrypted = f.read_bytes()

    with caplog.at_level(logging.INFO, logger="systemu.runtime.credentials.at_rest"):
        report = at_rest.reencrypt_plaintext_secrets(tmp_path)

    assert not backup.exists(), "a second start wrote a new backup"
    assert f.read_bytes() == encrypted, "a second start rewrote an already-encrypted file"
    assert report.reencrypted == () and report.backed_up == ()
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    assert first.backed_up == (".credentials.json.pre-encrypt.bak",)  # the first start did write one


def test_an_existing_backup_is_never_overwritten(tmp_path, seam):
    seam(_FakeDPAPI)
    f = tmp_path / ".credentials.json"
    _plant(f, {"k": SECRET})
    backup = tmp_path / (".credentials.json" + at_rest.BACKUP_SUFFIX)
    backup.write_bytes(b"the earliest copy")
    report = at_rest.reencrypt_plaintext_secrets(tmp_path)
    assert backup.read_bytes() == b"the earliest copy"
    assert report.backed_up == ()
    _assert_envelope_of(f, {"k": SECRET})


def test_without_a_working_envelope_nothing_is_touched(tmp_path, seam):
    seam(None)
    f = tmp_path / ".credentials.json"
    original = _plant(f, {"k": SECRET})
    report = at_rest.reencrypt_plaintext_secrets(tmp_path)
    assert f.read_bytes() == original
    assert not (tmp_path / (".credentials.json" + at_rest.BACKUP_SUFFIX)).exists()
    assert report.reencrypted == () and report.envelope.working is False


def test_an_unreadable_secret_file_is_left_alone(tmp_path, seam):
    seam(_FakeDPAPI)
    f = tmp_path / "secrets" / "api_token.json"
    f.parent.mkdir(parents=True)
    f.write_text("not json {", encoding="utf-8")
    report = at_rest.reencrypt_plaintext_secrets(tmp_path)
    assert f.read_text(encoding="utf-8") == "not json {"
    assert not list(tmp_path.rglob("*" + at_rest.BACKUP_SUFFIX))
    assert report.reencrypted == () and report.skipped


class _VaultRef:
    def __init__(self, root: Path):
        self.root = str(root)


def test_every_real_secret_writer_is_covered_end_to_end(tmp_path, seam):
    """Plaintext written by the REAL writers (seam absent) is re-encrypted by the
    daemon-start pass once the envelope works -- so the registry cannot miss a
    file a writer actually produces."""
    from systemu.runtime import dashboard_auth
    from systemu.runtime.credentials.store import CredentialStore
    from systemu.runtime.mcp.sdk.oauth import VaultTokenStore

    seam(None)
    cs = CredentialStore(base_dir=tmp_path)
    cs._keyring = None
    cs.set("openai_api_key", SECRET)
    VaultTokenStore(_VaultRef(tmp_path), "srv").save({"access_token": SECRET})
    dashboard_auth.set_passphrase(tmp_path, "a passphrase long enough")
    dashboard_auth.session_secret(tmp_path)
    dashboard_auth.mint_api_token(tmp_path)

    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")
                     if p.is_file() and not p.name.endswith(".credential_names.json"))
    assert len(written) == 5, written            # positive control: 5 secret files
    seam(_FakeDPAPI)
    report = at_rest.reencrypt_plaintext_secrets(tmp_path)
    assert sorted(report.reencrypted) == written
    for rel in written:
        assert at_rest._ENC_MARKER in (tmp_path / rel).read_text(encoding="utf-8"), rel
    assert sorted(report.backed_up) == sorted(r + at_rest.BACKUP_SUFFIX for r in written)


def test_the_writer_registry_names_every_module_that_writes_through_the_envelope():
    """A new caller of ``protect_json`` writes a file the start-up pass would not
    know about. This turns that into a red test instead of a plaintext file."""
    writers = set()
    for path in (REPO / "systemu").rglob("*.py"):
        if path.name == "at_rest.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and \
                    node.module.endswith("credentials.at_rest") and \
                    any(a.name == "protect_json" for a in node.names):
                writers.add(path.relative_to(REPO).as_posix())
    assert writers == set(at_rest.SECRET_FILE_WRITERS), (
        f"protect_json writers {sorted(writers)} != registry "
        f"{sorted(at_rest.SECRET_FILE_WRITERS)}: add the new file's location to "
        "at_rest.SECRET_FILE_PATTERNS and its module to SECRET_FILE_WRITERS")


# --------------------------------------------------------------------------- #
# (c) the daemon start actually runs it
# --------------------------------------------------------------------------- #

def test_the_daemon_start_hook_reencrypts_the_operating_vault(tmp_path, seam, monkeypatch):
    from systemu.scheduler import daemon as daemon_mod
    seam(_FakeDPAPI)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
    f = tmp_path / "connections" / "mcp_oauth" / "srv.json"
    _plant(f, {"access_token": SECRET})

    report = daemon_mod._reencrypt_plaintext_secrets_at_start()

    _assert_envelope_of(f, {"access_token": SECRET})
    # GATE-6 (S5 / R-A3): the hook's pass wrote the backup AND, its encrypted
    # file verified, deleted it.
    assert report.backed_up == ("connections/mcp_oauth/srv.json" + at_rest.BACKUP_SUFFIX,)
    assert report.backups_removed == report.backed_up
    assert not (f.parent / ("srv.json" + at_rest.BACKUP_SUFFIX)).exists()


def test_the_daemon_start_hook_never_raises(monkeypatch):
    from systemu.scheduler import daemon as daemon_mod

    def _boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(at_rest, "reencrypt_plaintext_secrets", _boom)
    assert daemon_mod._reencrypt_plaintext_secrets_at_start() is None


def test_the_daemon_loop_calls_the_start_hook():
    """Reachability pin: delete the call in `_run_daemon_loop` and this goes red."""
    import inspect
    from systemu.scheduler import daemon as daemon_mod
    src = inspect.getsource(daemon_mod._run_daemon_loop)
    assert "_reencrypt_plaintext_secrets_at_start(" in src
