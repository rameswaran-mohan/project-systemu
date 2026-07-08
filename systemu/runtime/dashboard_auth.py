"""R-SEC1 — dashboard authentication core (pure; no UI, no dashboard wiring).

The NiceGUI dashboard ships with NO authentication. This module is the pure
auth core that later tasks wire into the dashboard and Config. It stands alone
(imports cleanly on its own) and uses **stdlib only** — no new dependency.

The governing rule (fail-closed):
  * **loopback bind** (127.0.0.0/8, ::1, "localhost") -> auth is OPTIONAL; the
    dashboard may start and we merely WARN that it is unauthenticated.
  * **non-loopback bind** (0.0.0.0, ::, a LAN IP, …) -> auth is REQUIRED. If a
    passphrase is not configured, the dashboard REFUSES to start.

Security notes:
  * Passphrases are hashed with ``hashlib.scrypt`` (memory-hard KDF) over a
    per-hash 32-byte random salt; :func:`verify` is constant-time
    (``hmac.compare_digest``) and NEVER raises — any parse/format error is a
    fail-closed ``False``.
  * The secret at rest reuses the S5 DPAPI at-rest envelope
    (:mod:`systemu.runtime.credentials.at_rest`) when available (encrypted on
    Windows, where ``os.chmod(0600)`` is a no-op); otherwise it falls back to a
    plaintext JSON file guarded by a best-effort ``0600`` permission.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# tunables (no magic numbers)
# --------------------------------------------------------------------------- #
_SCRYPT_N = 2 ** 14          # CPU/memory cost
_SCRYPT_R = 8                # block size
_SCRYPT_P = 1                # parallelization
_SCRYPT_DKLEN = 32           # derived-key length (bytes)
_SCRYPT_SALT_BYTES = 32
# maxmem must admit n*r*p*128 bytes with headroom. n=2^14,r=8,p=1 needs ~128MB;
# 132MB gives margin so it runs across platforms/OpenSSL builds.
_SCRYPT_MAXMEM = 132 * 1024 * 1024
_SCHEME = "scrypt"

FAILURE_THRESHOLD = 5        # per-IP failures before that IP is locked out
# Global failures (across ALL IPs) within LOCKOUT_SECONDS before a distributed
# spray trips a global lock. Catches an attacker spraying one guess each from
# many IPs that never trip any per-IP counter.
GLOBAL_FAILURE_THRESHOLD = 20
LOCKOUT_SECONDS = 900        # 15 minutes

_ENV_HASH = "SYSTEMU_DASHBOARD_PASSPHRASE_HASH"
_SECRETS_DIRNAME = "secrets"
_AUTH_FILENAME = "dashboard_auth.json"
_SESSION_SECRET_FILENAME = "dashboard_session.secret"
_HASH_KEY = "passphrase_hash"
_SESSION_SECRET_KEY = "session_secret"

# reserved global-counter key in the lockout JSON (an IP can never equal it)
_GLOBAL_KEY = "__global__"

# auth-file states (Finding 4: absent must stay frictionless on loopback;
# present-but-corrupt must fail CLOSED).
_AUTH_ABSENT = "absent"
_AUTH_OK = "ok"
_AUTH_CORRUPT = "corrupt"


# --------------------------------------------------------------------------- #
# passphrase hashing / verification
# --------------------------------------------------------------------------- #

def hash_passphrase(pw: str) -> str:
    """Hash ``pw`` with scrypt over a fresh 32-byte random salt.

    Returns ``scrypt$14$8$1$<salt_hex>$<dk_hex>`` (the ``14`` is ``log2(n)``)."""
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        pw.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"{_SCHEME}${_SCRYPT_N.bit_length() - 1}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify(pw: str, stored: str) -> bool:
    """Constant-time verify of ``pw`` against a ``hash_passphrase`` string.

    Fail-closed: ANY parse/format/type error returns ``False`` and never raises.
    """
    try:
        parts = stored.split("$")
        if len(parts) != 6:
            return False
        scheme, log_n, r, p, salt_hex, dk_hex = parts
        if scheme != _SCHEME:
            return False
        n = 2 ** int(log_n)
        r = int(r)
        p = int(p)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        candidate = hashlib.scrypt(
            pw.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(candidate, expected)
    except Exception:
        # bad hex, wrong field count, non-int params, None stored, etc.
        return False


# --------------------------------------------------------------------------- #
# loopback classification + start verdict
# --------------------------------------------------------------------------- #

def is_loopback(host: str) -> bool:
    """True iff ``host`` binds only the loopback interface.

    ``"localhost"`` (case/space-insensitive) or any address whose
    ``ipaddress`` form ``.is_loopback``. Non-parseable / empty / ``0.0.0.0`` /
    ``::`` -> ``False``.
    """
    try:
        h = host.strip().lower()
        if h == "localhost":
            return True
        return ipaddress.ip_address(h).is_loopback
    except Exception:
        return False


@dataclass(frozen=True)
class StartVerdict:
    """Whether the dashboard may start, and under what auth posture."""
    may_start: bool
    require_auth: bool
    warn: bool
    reason: str = ""


def exposure_check(host: str, configured: bool) -> StartVerdict:
    """Decide the dashboard's start posture for a bind ``host``.

    | host       | configured | may_start | require_auth | warn |
    |------------|-----------|-----------|--------------|------|
    | loopback   | no        | yes       | no           | yes  |
    | loopback   | yes       | yes       | yes          | no   |
    | non-loop   | yes       | yes       | yes          | no   |
    | non-loop   | no        | NO (reason)                       |
    """
    loop = is_loopback(host)
    if loop and not configured:
        return StartVerdict(may_start=True, require_auth=False, warn=True)
    if loop and configured:
        return StartVerdict(may_start=True, require_auth=True, warn=False)
    if not loop and configured:
        return StartVerdict(may_start=True, require_auth=True, warn=False)
    # non-loopback + not configured -> fail closed
    reason = (
        f"Refusing to start the dashboard: bind host {host!r} is NOT loopback, "
        f"so a passphrase is REQUIRED but none is configured. Set "
        f"{_ENV_HASH} (or run `systemu doctor --set-passphrase`), "
        f"or bind 127.0.0.1 to run unauthenticated on loopback only."
    )
    return StartVerdict(may_start=False, require_auth=True, warn=False, reason=reason)


# --------------------------------------------------------------------------- #
# vault secret storage (reuses S5 at-rest envelope when available)
# --------------------------------------------------------------------------- #

def _secrets_dir(vault) -> Path:
    return Path(vault) / _SECRETS_DIRNAME


def _auth_file(vault) -> Path:
    return _secrets_dir(vault) / _AUTH_FILENAME


def _protect(obj: Any) -> str:
    """Serialize ``obj`` for disk, encrypting at rest via S5 DPAPI where
    available; plaintext JSON otherwise (guarded by a 0600 file perm)."""
    try:
        from systemu.runtime.credentials.at_rest import protect_json
        return protect_json(obj)
    except Exception:
        return json.dumps(obj)


def _unprotect(text: str) -> Any:
    """Inverse of :func:`_protect`; reads a DPAPI envelope OR legacy plaintext."""
    try:
        from systemu.runtime.credentials.at_rest import unprotect_json
        return unprotect_json(text)
    except Exception:
        return json.loads(text)


def _write_secret_file(path: Path, obj: Any) -> None:
    """Write ``obj`` to ``path`` atomically-ish (temp + replace) with 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_protect(obj), encoding="utf-8")
    _chmod_600(tmp)
    os.replace(tmp, path)
    _chmod_600(path)


def _read_secret_file(path: Path) -> dict:
    """Defensive read: corrupt/missing -> {} (never raises)."""
    try:
        if path.exists():
            data = _unprotect(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("[DashboardAuth] failed to read %s: %s", path, exc)
    return {}


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:  # pragma: no cover - Windows / non-posix no-op
        pass


def set_passphrase(vault, pw: str) -> None:
    """Store ``hash_passphrase(pw)`` in the vault secret file."""
    _write_secret_file(_auth_file(vault), {_HASH_KEY: hash_passphrase(pw)})


def get_passphrase_hash_vault(vault) -> Optional[str]:
    """The stored passphrase hash, or ``None`` if unset/absent/corrupt.

    A corrupt file yields ``None`` here (nobody can log in until the operator
    fixes it — the correct hard-fail-closed for :func:`verify`); the "configured"
    predicates below still report True for a corrupt file so auth stays REQUIRED.
    """
    h = _read_secret_file(_auth_file(vault)).get(_HASH_KEY)
    return h if isinstance(h, str) and h else None


def get_active_passphrase_hash(config, vault) -> Optional[str]:
    """The hash to VERIFY against: env var (SYSTEMU_DASHBOARD_PASSPHRASE_HASH) takes
    precedence, else the vault-stored hash. None if neither is set.
    (config is accepted for symmetry with is_configured; the env is read directly.)

    Finding A: the login page must resolve the SAME hash the exposure gate armed
    on. The env-only Docker path (env hash set, no vault file) armed the guard via
    :func:`is_configured` but had no vault hash to verify against, so login was
    permanently impossible. This unifies the "verifiable at login" predicate with
    "configured for the gate" for every VALID case.

    The one intentional divergence from :func:`is_configured` is the corrupt-vault
    case: :func:`is_configured` stays True (fail-closed — the guard stays armed)
    while this returns None (nobody can log in until the file is repaired — the
    correct HARD fail-closed).
    """
    env = os.getenv(_ENV_HASH)
    if env:
        return env
    return get_passphrase_hash_vault(vault)


def _auth_file_state(vault) -> str:
    """Classify the vault auth file as absent / ok / corrupt (never raises).

    * ``absent``  — no file on disk (loopback frictionless default is preserved).
    * ``ok``      — parses to a dict carrying a non-empty ``passphrase_hash``.
    * ``corrupt`` — a file EXISTS but does not parse to a usable hash (truncated,
      wrong shape, undecryptable envelope, …). This must fail CLOSED.
    """
    path = _auth_file(vault)
    if not path.exists():
        return _AUTH_ABSENT
    try:
        data = _unprotect(path.read_text(encoding="utf-8"))
    except Exception:
        return _AUTH_CORRUPT
    if isinstance(data, dict):
        h = data.get(_HASH_KEY)
        if isinstance(h, str) and h:
            return _AUTH_OK
    return _AUTH_CORRUPT


def is_configured_vault(vault) -> bool:
    """True iff the vault auth file is present (configured), even if corrupt.

    An ``ok`` file is configured. A present-but-``corrupt`` file is ALSO reported
    as configured (fail-closed: keep auth REQUIRED) and logs a loud, actionable
    error. Only a genuinely ABSENT file returns False, preserving the loopback
    frictionless default.
    """
    state = _auth_file_state(vault)
    if state == _AUTH_CORRUPT:
        logger.error(
            "[DashboardAuth] the dashboard passphrase file %s is present but "
            "UNREADABLE/CORRUPT. Failing CLOSED: auth stays REQUIRED and no "
            "login can succeed until you repair or remove it (e.g. re-run "
            "`systemu doctor --set-passphrase`, or delete the file to reset to "
            "the loopback-only unauthenticated default).",
            _auth_file(vault),
        )
        return True
    return state == _AUTH_OK


def is_configured(config, vault) -> bool:
    """True iff a passphrase hash is configured via the env var OR the vault.

    ``config`` is accepted for a future task that may expose
    ``dashboard_passphrase_hash``; this module reads the env directly so it
    stands alone. A present-but-corrupt vault file counts as configured
    (fail-closed) via :func:`is_configured_vault`.
    """
    if os.getenv(_ENV_HASH):
        return True
    return is_configured_vault(vault)


# --------------------------------------------------------------------------- #
# capability row (privacy / health surface)
# --------------------------------------------------------------------------- #

_ENV_TLS_CERT = "SYSTEMU_TLS_CERT"
_ENV_TLS_KEY = "SYSTEMU_TLS_KEY"


def capability_row(config, vault, host: str) -> dict:
    """Deterministic auth/TLS capability state for the profile / privacy page.

    Pure (no side effects) — reads only :func:`is_configured` (env + vault),
    :func:`is_loopback`, and the two TLS env vars, so the privacy/health surface
    (COMPLIANCE-SPEC §CMP-0.a design 7 / UX-6) can render the dashboard's auth
    and TLS posture without any I/O of its own.

    * ``dashboard_auth``: ``"session"`` when a passphrase is configured, else
      ``"none(loopback-only)"`` (loopback is the only place unauthenticated is
      permitted).
    * ``tls``: ``"n/a(loopback)"`` on a loopback bind (no transport exposure);
      otherwise ``"on"`` iff BOTH the cert AND key env vars are set, else
      ``"off"``.
    """
    dashboard_auth = "session" if is_configured(config, vault) else "none(loopback-only)"
    if is_loopback(host):
        tls = "n/a(loopback)"
    elif os.getenv(_ENV_TLS_CERT) and os.getenv(_ENV_TLS_KEY):
        tls = "on"
    else:
        tls = "off"
    return {"dashboard_auth": dashboard_auth, "tls": tls}


# --------------------------------------------------------------------------- #
# session secret (stable, persisted)
# --------------------------------------------------------------------------- #

def session_secret(vault) -> str:
    """Get-or-create a persisted 64-hex-char session secret for ``vault``.

    The secret is stored through the SAME S5 at-rest envelope as the passphrase
    hash (``{"session_secret": secret}`` via :func:`_write_secret_file`), so a
    local process/backup that reads the vault dir cannot recover the raw
    session-signing key and forge dashboard sessions.

    Stable across calls; defensive — a corrupt/empty/legacy-plaintext file that
    fails the dict parse (or holds a <32-char secret) is simply regenerated. On
    upgrade this invalidates any pre-existing sessions, which is acceptable.
    """
    path = _secrets_dir(vault) / _SESSION_SECRET_FILENAME
    try:
        existing = _read_secret_file(path).get(_SESSION_SECRET_KEY)
        if isinstance(existing, str) and len(existing) >= 32:
            return existing
    except Exception as exc:
        logger.warning("[DashboardAuth] failed to read session secret %s: %s", path, exc)
    secret = secrets.token_hex(32)  # 64 hex chars
    try:
        _write_secret_file(path, {_SESSION_SECRET_KEY: secret})
    except Exception as exc:  # pragma: no cover - disk failure
        logger.warning("[DashboardAuth] failed to persist session secret %s: %s", path, exc)
    return secret


# --------------------------------------------------------------------------- #
# per-IP lockout
# --------------------------------------------------------------------------- #

class LockoutStore:
    """JSON-persisted failed-login lockout — per-IP AND global.

    Shape: ``{ip: {"fails": int, "until": epoch_or_0}}`` plus a reserved
    ``"__global__"`` entry of the same shape that counts failures across ALL
    IPs. After :data:`FAILURE_THRESHOLD` failures an IP is locked for
    :data:`LOCKOUT_SECONDS`; after :data:`GLOBAL_FAILURE_THRESHOLD` failures
    within the same window the whole dashboard is globally locked (defeats a
    distributed spray that never trips any single per-IP counter).

    Window-expiry reset: once an entry's lockout window has elapsed the counter
    resets to a fresh window on the next failure, so the effective threshold
    stays N per window rather than collapsing to 1 after the first lockout.

    All reads are defensive (corrupt/missing -> empty, never raises); writes are
    atomic-ish (temp + replace).
    """

    def __init__(self, path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("[DashboardAuth] corrupt lockout file %s: %s", self.path, exc)
        return {}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:  # pragma: no cover - disk failure
            logger.warning("[DashboardAuth] failed to write lockout file %s: %s", self.path, exc)

    @staticmethod
    def _entry_locked(entry: Any, now: float) -> bool:
        if not isinstance(entry, dict):
            return False
        try:
            return float(entry.get("until", 0)) > now
        except Exception:
            return False

    @staticmethod
    def _bump(entry: Any, threshold: int, now: float) -> dict:
        """Increment a ``{"fails","until"}`` entry, resetting first if its prior
        lockout window has expired, and (re)arming ``until`` at the threshold."""
        if not isinstance(entry, dict):
            entry = {"fails": 0, "until": 0}
        # Finding 3: a set-but-expired window resets the counter BEFORE the bump
        # so each expiry starts a fresh N-strike window.
        try:
            until = float(entry.get("until", 0))
        except Exception:
            until = 0.0
        if until and until <= now:
            entry = {"fails": 0, "until": 0}
        try:
            fails = int(entry.get("fails", 0)) + 1
        except Exception:
            fails = 1
        entry["fails"] = fails
        entry["until"] = now + LOCKOUT_SECONDS if fails >= threshold else 0
        return entry

    def is_locked(self, ip: str) -> bool:
        return self._entry_locked(self._load().get(ip), time.time())

    def is_globally_locked(self) -> bool:
        """True iff the global failure count crossed :data:`GLOBAL_FAILURE_THRESHOLD`
        within :data:`LOCKOUT_SECONDS` (a distributed spray across many IPs)."""
        return self._entry_locked(self._load().get(_GLOBAL_KEY), time.time())

    def record_failure(self, ip: str) -> None:
        now = time.time()
        data = self._load()
        data[ip] = self._bump(data.get(ip), FAILURE_THRESHOLD, now)
        # Finding 2: every failure also increments the global counter.
        data[_GLOBAL_KEY] = self._bump(data.get(_GLOBAL_KEY), GLOBAL_FAILURE_THRESHOLD, now)
        self._save(data)

    def record_success(self, ip: str) -> None:
        """Clear the per-IP counter. The GLOBAL counter is deliberately NOT
        wiped — a single success must not reopen a distributed spray (it decays
        only via the window-expiry reset in :meth:`_bump`)."""
        data = self._load()
        if ip in data and ip != _GLOBAL_KEY:
            del data[ip]
            self._save(data)
