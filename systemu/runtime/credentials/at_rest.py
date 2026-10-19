"""S5 — at-rest encryption for secret files (spec UNIFIED-v2 §7 / §11.3).

`os.chmod(0o600)` is a NO-OP on Windows (the primary platform), so any secret
written as plaintext JSON — OAuth tokens (`VaultTokenStore`), the credential
FILE FALLBACK (`.credentials.json`), and later the captured Playwright
`storage_state` — sits readable on disk. This module wraps such files in a
DPAPI envelope on Windows.

Design:
  * **Windows:** `win32crypt.CryptProtectData` (DPAPI, USER-scope, UI-forbidden).
    Encryption is bound to the daemon's user identity — a different token (e.g.
    a future AppContainer child, §S2) cannot decrypt it.
  * **POSIX / no-DPAPI:** plaintext JSON, unchanged — there the existing
    `0o600` permission IS a real control. No new behavior, no regression.
  * **Transparent + migrate-on-read:** `unprotect_json` reads a legacy plaintext
    file as-is; the next `save` re-writes it as an envelope. No token is ever
    lost during the upgrade.
  * **Fail-safe:** a genuinely corrupt/undecryptable payload raises `ValueError`;
    the callers (the stores) already wrap load in try/except and return `{}` (a
    re-prompt), never crashing boot.

2d -- WHAT MAY CLAIM "ENCRYPTED" (truthfulness):
  * :func:`envelope_status` is the ONE answer to "is the envelope working here".
    It is a real protect/unprotect round trip of a throwaway probe value through
    the very functions every secret goes through -- not an import check. An
    importable `win32crypt` whose calls fail, or an "envelope" that leaves the
    value readable, is NOT working, and says why. :func:`is_encrypted_at_rest`
    and the platform profile's secrets row both derive from it.
  * :func:`reencrypt_plaintext_secrets` runs at daemon start: every plaintext
    secret file the product writes (:data:`SECRET_FILE_PATTERNS`) is
    re-encrypted after a ONE-TIME `<file>.pre-encrypt.bak` copy of the original
    bytes (0600 where the OS has modes). The new envelope replaces the file
    only once it has been read back from disk and decrypts to exactly the
    original bytes, and the backup is then deleted if it holds exactly those
    bytes; otherwise the plaintext file and its backup stay. A backup is a
    PLAINTEXT copy of a secret: :func:`plaintext_backups` lists any that remain
    for the doctor, and ``.gitignore`` covers them. Idempotent: an envelope is
    never rewritten, an existing backup is never overwritten.

Leaf module: imports only stdlib + a lazy `win32crypt`; no systemu imports, so
it is import-cycle-free.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)

# JSON key that marks an encrypted envelope. Deliberately unlikely to collide
# with any real secret-dict key.
_ENC_MARKER = "__systemu_at_rest__"
_SCHEME_DPAPI = "dpapi"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_DESCRIPTION = "systemu-secret"


def _win32crypt():
    """Return the win32crypt module if DPAPI is usable, else None. Lazy so a
    non-Windows / no-pywin32 environment simply falls back to plaintext."""
    try:
        import win32crypt  # type: ignore
        return win32crypt
    except Exception:
        return None


@dataclass(frozen=True)
class EnvelopeStatus:
    """Whether the at-rest envelope WORKS on this machine, and if not, why.

    ``reason`` is ASCII and empty exactly when ``working`` is True.
    """

    working: bool
    reason: str = ""


#: Why there is no envelope when the seam is absent. Worded for the operator:
#: on Windows it is the fix; elsewhere it is simply true.
REASON_NO_PYWIN32 = "win32crypt (pywin32) is not importable"


def envelope_status() -> EnvelopeStatus:
    """A REAL protect/unprotect round trip through the seam secrets use.

    A claim only a working envelope can earn: the probe value must come back
    wrapped in the envelope marker, must NOT be readable in the protected text,
    and must decrypt to exactly itself. Never raises.
    """
    if _win32crypt() is None:
        return EnvelopeStatus(False, REASON_NO_PYWIN32)
    canary = "systemu-envelope-probe-" + secrets.token_hex(8)
    try:
        text = protect_json({"probe": canary})
    except Exception as exc:
        return EnvelopeStatus(False, f"DPAPI protect failed ({type(exc).__name__})")
    # Readable means readable in the bytes ON DISK -- the base64 blob included.
    # An envelope whose blob is the plaintext merely base64-encoded is not
    # encryption, and checking the outer text alone would let it pass.
    try:
        outer = json.loads(text)
        blob = base64.b64decode(outer["data"]) if type(outer) is dict else b""
    except Exception:
        outer, blob = None, b""
    if (type(text) is not str or canary in text or not _is_envelope(outer)
            or not blob or canary.encode("utf-8") in blob):
        return EnvelopeStatus(
            False, "the envelope did not encrypt (the probe value was readable)")
    try:
        back = unprotect_json(text)
    except Exception as exc:
        return EnvelopeStatus(False, f"DPAPI unprotect failed ({type(exc).__name__})")
    if type(back) is not dict or type(back.get("probe")) is not str \
            or back["probe"] != canary:
        return EnvelopeStatus(False, "the DPAPI round trip returned a different value")
    return EnvelopeStatus(True, "")


def is_encrypted_at_rest() -> bool:
    """True iff at-rest secrets are DPAPI-encrypted on this machine -- i.e. the
    envelope round-trips (:func:`envelope_status`). When False, secrets are
    plaintext (under a POSIX 0600 file, or unprotected on Windows)."""
    return envelope_status().working


def protect_json(obj: Any) -> str:
    """Serialize ``obj`` to the string to write to disk.

    Windows: an encrypted envelope ``{_ENC_MARKER: "dpapi", "data": <b64>}``.
    Elsewhere: plaintext JSON (unchanged; the 0600 file perm is the control)."""
    payload = json.dumps(obj)
    wc = _win32crypt()
    if wc is None:
        return payload
    return _seal_text(payload, wc)


def _seal_text(payload: str, wc) -> str:
    """The envelope around exactly ``payload`` (JSON text): decrypting it gives
    back ``payload``'s UTF-8 bytes, byte for byte."""
    blob = wc.CryptProtectData(
        payload.encode("utf-8"), _DESCRIPTION, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN,
    )
    return json.dumps({
        _ENC_MARKER: _SCHEME_DPAPI,
        "data": base64.b64encode(bytes(blob)).decode("ascii"),
    })


def unprotect_json(text: str) -> Any:
    """Inverse of :func:`protect_json`.

    Handles three inputs transparently: an encrypted envelope; a legacy
    plaintext JSON value (migrate-on-read — returned as-is so the next write can
    upgrade it); anything else raises so the caller can fail safe."""
    data = json.loads(text)  # may raise json.JSONDecodeError -> caller returns {}
    if isinstance(data, dict) and data.get(_ENC_MARKER) == _SCHEME_DPAPI:
        wc = _win32crypt()
        if wc is None:
            # envelope written on a DPAPI machine but read where DPAPI is absent
            raise ValueError("encrypted secret cannot be decrypted (DPAPI unavailable)")
        blob = base64.b64decode(data["data"])
        clear = wc.CryptUnprotectData(blob, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN)
        return json.loads(clear[1].decode("utf-8"))
    # legacy plaintext (or any non-envelope JSON value)
    return data


# --------------------------------------------------------------------------- #
# 2d -- proactive re-encryption of plaintext secrets (daemon start)
# --------------------------------------------------------------------------- #

#: Every vault-relative location the product writes through :func:`protect_json`.
#: A file a writer produces that is not matched here would stay plaintext
#: forever; ``tests/test_secrets_envelope_truth.py`` writes one secret through
#: each real writer and fails if this pass misses any of them.
SECRET_FILE_PATTERNS: Tuple[str, ...] = (
    ".credentials.json",                   # CredentialStore file fallback
    "secrets/dashboard_auth.json",         # dashboard passphrase hash
    "secrets/dashboard_session.secret",    # dashboard session secret
    "secrets/api_token.json",              # dashboard API token hash
    "connections/mcp_oauth/*.json",        # MCP OAuth token stores
)

#: The modules that write those files (repo-relative). A new
#: ``protect_json`` caller must be registered here AND in the patterns above;
#: the same test fails until it is.
SECRET_FILE_WRITERS: Tuple[str, ...] = (
    "systemu/runtime/credentials/store.py",
    "systemu/runtime/dashboard_auth.py",
    "systemu/runtime/mcp/sdk/oauth.py",
)

#: The one-time copy of a plaintext secret file, taken before it is encrypted.
BACKUP_SUFFIX = ".pre-encrypt.bak"


@dataclass(frozen=True)
class ReencryptReport:
    """What one start-up pass did. Paths are vault-relative POSIX strings."""

    envelope: EnvelopeStatus
    reencrypted: Tuple[str, ...] = ()
    backed_up: Tuple[str, ...] = ()
    #: ``"<path>: <reason>"`` for a secret file that was left exactly as found.
    skipped: Tuple[str, ...] = ()
    #: ``"<path>: <reason>"`` for a plaintext secret whose new envelope, read back
    #: from disk, did NOT decrypt to exactly its original bytes. The plaintext
    #: file was left in place and its backup kept; the doctor names the backup.
    unverified: Tuple[str, ...] = ()
    #: Backups deleted this pass: the encrypted file verified AND the backup
    #: held exactly the original bytes (``backed_up`` still lists the ones this
    #: pass wrote, deleted or not).
    backups_removed: Tuple[str, ...] = ()


def secret_files(vault_root) -> List[Path]:
    """Every existing file under ``vault_root`` matched by the patterns."""
    root = Path(vault_root)
    out: List[Path] = []
    for pattern in SECRET_FILE_PATTERNS:
        for p in sorted(root.glob(pattern)):
            if p.is_file() and p not in out:
                out.append(p)
    return out


def _is_envelope(data: Any) -> bool:
    return type(data) is dict and data.get(_ENC_MARKER) == _SCHEME_DPAPI


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - a filesystem without modes
        pass


def _write_new_0600(path: Path, data: bytes) -> None:
    """Create ``path`` (it must not exist) already restricted to 0600, so the
    copy is never readable to others even for an instant on POSIX."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    _chmod_600(path)


def _write_0600(path: Path, text: str) -> None:
    """Create or truncate ``path`` restricted to 0600 and write ``text``."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8"))
    _chmod_600(path)


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _unverified_reason(path: Path, original: bytes, wc) -> str:
    """``""`` iff the encrypted file at ``path``, READ BACK FROM DISK, is an
    envelope that decrypts to exactly ``original``; otherwise why not.

    The check that stands between a plaintext secret and its replacement: an
    in-memory round trip of a re-serialised value cannot see what reached the
    disk, nor a DPAPI that decrypts to something else. ASCII; never a value.
    """
    try:
        outer = json.loads(path.read_bytes().decode("utf-8"))
    except Exception as exc:
        return f"the encrypted file did not read back ({type(exc).__name__})"
    if not _is_envelope(outer) or type(outer.get("data")) is not str:
        return "the written file is not an envelope"
    try:
        clear = wc.CryptUnprotectData(base64.b64decode(outer["data"]), None, None,
                                      None, _CRYPTPROTECT_UI_FORBIDDEN)[1]
        clear = bytes(clear)
    except Exception as exc:
        return f"the encrypted file did not decrypt ({type(exc).__name__})"
    if not hmac.compare_digest(clear, original):
        return "the encrypted file decrypts to different bytes than the original"
    return ""


def reencrypt_plaintext_secrets(vault_root, *, log=None) -> ReencryptReport:
    """Encrypt every plaintext secret file under ``vault_root`` in place.

    Before the envelope existed (and whenever it was unavailable) secrets were
    written as plaintext JSON, and they used to be upgraded only on their NEXT
    write -- a token nobody rotates stayed readable forever. This pass, run at
    daemon start, upgrades them now:

      * no working envelope -> nothing is touched (there is nothing to encrypt
        WITH); on Windows the remaining plaintext is logged as a warning;
      * an unreadable / non-JSON file -> left exactly as found, reported;
      * an envelope -> left alone (idempotent);
      * plaintext -> a ONE-TIME ``<file>.pre-encrypt.bak`` of the original
        bytes is created (never overwritten if it exists); the envelope of
        exactly the original text is written beside the file, READ BACK FROM
        DISK and required to decrypt to exactly the original bytes, and only
        then atomically swapped in. The backup is then DELETED when it holds
        exactly those original bytes (``backups_removed``); a backup with any
        other bytes is never deleted. If the envelope does not verify, the
        plaintext file is left in place untouched, the backup is kept, and the
        failure is reported (``unverified``) and logged naming the backup --
        the doctor names it too (``plaintext_backups``).

    One INFO line per pass that changed something, naming files, never values.
    Never raises for a single bad file; the caller guards the pass as a whole.
    """
    log = log or logger
    root = Path(vault_root)
    status = envelope_status()
    files = secret_files(root)

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    if not status.working:
        if files and sys.platform.startswith("win"):
            log.warning(
                "[S5] %d secret file(s) under %s cannot be encrypted at rest: the "
                "DPAPI envelope is not working (%s). They are re-encrypted at the "
                "next daemon start once it is.", len(files), root, status.reason)
        return ReencryptReport(envelope=status)

    wc = _win32crypt()
    reencrypted: List[str] = []
    backed_up: List[str] = []
    backups_removed: List[str] = []
    skipped: List[str] = []
    unverified: List[str] = []
    for path in files:
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            skipped.append(f"{rel(path)}: unreadable ({type(exc).__name__})")
            continue
        if _is_envelope(data):
            continue
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        tmp = path.with_name(path.name + ".reencrypt.tmp")
        try:
            # Seal the original TEXT, not a re-serialised value, so the envelope
            # decrypts to exactly the bytes being replaced.
            text = _seal_text(raw.decode("utf-8"), wc)
            if not backup.exists():
                _write_new_0600(backup, raw)
                backed_up.append(rel(backup))
            _write_0600(tmp, text)
            why = _unverified_reason(tmp, raw, wc)
            if why:
                _discard(tmp)
                unverified.append(f"{rel(path)}: {why}")
                log.warning(
                    "[S5] %s was NOT re-encrypted: %s. It is still plaintext, and "
                    "its plaintext backup is kept at %s. Restart the daemon to "
                    "retry; `systemu doctor` lists every plaintext backup.",
                    rel(path), why, backup)
                continue
            os.replace(tmp, path)
            _chmod_600(path)
        except Exception as exc:
            _discard(tmp)
            skipped.append(f"{rel(path)}: {type(exc).__name__}")
            continue
        reencrypted.append(rel(path))
        # The encrypted file VERIFIED (it decrypts to exactly `raw`), so a
        # backup holding exactly `raw` is now only a plaintext copy of a secret
        # that is safe on disk: delete it. A backup with any other bytes (an
        # earlier copy) is never deleted -- the envelope cannot restore it.
        if _backup_matches(backup, raw):
            try:
                backup.unlink()
                backups_removed.append(rel(backup))
            except OSError as exc:
                log.warning("[S5] the plaintext backup %s could not be deleted "
                            "(%s); `systemu doctor` lists it.", backup,
                            type(exc).__name__)

    if reencrypted:
        log.info(
            "[S5] re-encrypted %d plaintext secret file(s) at rest under %s: %s. "
            "One-time plaintext backup(s) written: %s; deleted once the encrypted "
            "file verified: %s",
            len(reencrypted), root, ", ".join(reencrypted),
            ", ".join(backed_up) or "none (a backup already existed)",
            ", ".join(backups_removed) or "none")
    for item in skipped:
        log.warning("[S5] secret file left as found: %s", item)
    return ReencryptReport(envelope=status, reencrypted=tuple(reencrypted),
                           backed_up=tuple(backed_up), skipped=tuple(skipped),
                           unverified=tuple(unverified),
                           backups_removed=tuple(backups_removed))


def _backup_matches(backup: Path, original: bytes) -> bool:
    """True iff ``backup`` holds exactly ``original`` -- the bytes the verified
    envelope decrypts to. Anything else (unreadable, different) is False, so
    the backup is kept."""
    try:
        held = backup.read_bytes()
    except OSError:
        return False
    return type(held) is bytes and hmac.compare_digest(held, original)


#: What a plaintext backup's secret file is now (``plaintext_backups``).
SECRET_ENCRYPTED = "encrypted"
SECRET_PLAINTEXT = "plaintext"
SECRET_MISSING = "missing"


def plaintext_backups(vault_root) -> List[dict]:
    """Every ``<secret file>.pre-encrypt.bak`` under ``vault_root``.

    Each is a PLAINTEXT copy of a secret. Returns ``{"backup", "secret_file",
    "relative", "secret_state"}`` per backup: absolute paths, the vault-relative
    backup path, and whether the secret file it copies is now an envelope
    (``encrypted``), anything else (``plaintext``), or gone (``missing``). Reads
    only the JSON marker -- never decrypts, never returns a value.
    """
    root = Path(vault_root)
    out: List[dict] = []
    for pattern in SECRET_FILE_PATTERNS:
        for backup in sorted(root.glob(pattern + BACKUP_SUFFIX)):
            if not backup.is_file():
                continue
            secret = backup.with_name(backup.name[:-len(BACKUP_SUFFIX)])
            if not secret.is_file():
                state = SECRET_MISSING
            else:
                try:
                    data = json.loads(secret.read_bytes().decode("utf-8"))
                except Exception:
                    data = None
                state = SECRET_ENCRYPTED if _is_envelope(data) else SECRET_PLAINTEXT
            entry = {"backup": str(backup), "secret_file": str(secret),
                     "relative": backup.relative_to(root).as_posix(),
                     "secret_state": state}
            if entry not in out:
                out.append(entry)
    return out
