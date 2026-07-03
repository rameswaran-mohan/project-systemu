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

Leaf module: imports only stdlib + a lazy `win32crypt`; no systemu imports, so
it is import-cycle-free.
"""
from __future__ import annotations

import base64
import json
from typing import Any

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


def is_encrypted_at_rest() -> bool:
    """True iff at-rest secrets are DPAPI-encrypted on this machine (Windows +
    win32crypt). When False, secrets are plaintext under a POSIX 0600 file."""
    return _win32crypt() is not None


def protect_json(obj: Any) -> str:
    """Serialize ``obj`` to the string to write to disk.

    Windows: an encrypted envelope ``{_ENC_MARKER: "dpapi", "data": <b64>}``.
    Elsewhere: plaintext JSON (unchanged; the 0600 file perm is the control)."""
    payload = json.dumps(obj)
    wc = _win32crypt()
    if wc is None:
        return payload
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
