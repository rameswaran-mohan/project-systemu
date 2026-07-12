"""v0.8.18 — secure credential storage (OS keychain via keyring, 0600-file fallback)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
_SERVICE = "systemu"


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return "<none>"
    return f"<redacted:{value[-4:]}>" if len(value) >= 4 else "<redacted>"


def usable_keyring():
    """Return the ``keyring`` module iff a REAL OS keyring backend is usable on
    this host, else ``None``.

    ``keyring.get_keyring()`` does not raise on a headless box with no
    Keychain/SecretService — it returns a ``fail``/``null`` sentinel backend
    that only raises at get/set time. Treating that sentinel as "present" would
    (a) route every secret through a doomed set() that then falls back anyway,
    and (b) make the profile report a keyring that cannot actually hold a
    secret. So we reject the sentinel backends here — this is the single
    source-of-truth probe consumed by both the store and ``platform_profile``.
    Import-guarded: never raises.
    """
    try:
        import keyring
        backend = keyring.get_keyring()
    except Exception:
        return None
    module = (type(backend).__module__ or "").lower()
    if "fail" in module or "null" in module:
        return None
    try:
        if float(getattr(backend, "priority", 1)) <= 0:
            return None
    except Exception:
        pass
    return keyring


class CredentialStore:
    """keyring-backed secret store; falls back to a 0600 JSON file under the vault dir."""

    def __init__(self, base_dir=None):
        self._base = Path(base_dir or os.getenv("SYSTEMU_VAULT_DIR", "systemu/vault"))
        self._keyring = self._init_keyring()

    def _init_keyring(self):
        # DEP-1/6: prefer the OS keyring. usable_keyring() rejects the fail/null
        # sentinel backends so a secret is NEVER routed through a doomed keyring
        # write when there is no real backend — the flagged plaintext fallback
        # is used instead. The warning FLAGS that last-resort loudly.
        kr = usable_keyring()
        if kr is None:
            logger.warning(
                "[Credentials] no usable OS keyring backend — secrets fall back "
                "to a flagged at-rest file (DPAPI on Windows, 0600 plaintext on POSIX)")
        return kr

    @property
    def _file(self) -> Path:
        return self._base / ".credentials.json"

    @property
    def _names_file(self) -> Path:
        # T1 (spec §5.10): a NAMES-ONLY registry so OnTheTable can project which
        # credentials exist. The keyring backend cannot enumerate its entries, so
        # names are tracked here on set/delete. NEVER contains a secret value.
        return self._base / ".credential_names.json"

    def _read_names(self) -> list:
        try:
            if self._names_file.exists():
                data = json.loads(self._names_file.read_text(encoding="utf-8"))
                return [n for n in data if isinstance(n, str)] if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def _write_names(self, names: list) -> None:
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            self._names_file.write_text(json.dumps(sorted(set(names))), encoding="utf-8")
        except Exception as exc:  # the name registry must never break credential I/O
            logger.debug("[Credentials] name registry write failed: %s", exc)

    def _record_name(self, key: str) -> None:
        names = self._read_names()
        if key not in names:
            self._write_names(names + [key])

    def _forget_name(self, key: str) -> None:
        names = self._read_names()
        if key in names:
            self._write_names([n for n in names if n != key])

    def list_names(self) -> list:
        """The registered credential NAMES (no values) — the OnTheTable projection
        surface (keyring cannot enumerate its own entries)."""
        return sorted(self._read_names())

    def get(self, key: str) -> Optional[str]:
        if self._keyring is not None:
            try:
                v = self._keyring.get_password(_SERVICE, key)
                if v:
                    return v
            except Exception as exc:
                logger.warning("[Credentials] keyring get failed: %s", exc)
        return self._read_file().get(key)

    def set(self, key: str, value: str) -> str:
        if self._keyring is not None:
            try:
                self._keyring.set_password(_SERVICE, key, value)
                return "keyring"
            except Exception as exc:
                logger.warning("[Credentials] keyring set failed: %s", exc)
            finally:
                self._record_name(key)
        data = self._read_file()
        data[key] = value
        self._write_file(data)
        self._record_name(key)
        return "file"

    def delete(self, key: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(_SERVICE, key)
            except Exception as exc:
                logger.warning("[Credentials] keyring delete failed: %s", exc)
        data = self._read_file()
        if key in data:
            del data[key]
            self._write_file(data)
        self._forget_name(key)

    def status(self, key: str) -> dict:
        v = self.get(key)
        return {"present": v is not None,
                "last4": (v[-4:] if v else None),
                "backend": "keyring" if self._keyring is not None else "file"}

    def _read_file(self) -> dict:
        try:
            if self._file.exists():
                from systemu.runtime.credentials.at_rest import unprotect_json
                # S5: decrypts a DPAPI envelope AND reads a legacy plaintext
                # fallback file transparently (migrate-on-read).
                data = unprotect_json(self._file.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            # A corrupt/unreadable store must not masquerade as "no credentials"
            # — that silently hides real secrets and may trigger re-provisioning.
            logger.warning("[Credentials] failed to read credential file %s: %s",
                           self._file, exc)
        return {}

    def _write_file(self, data: dict) -> None:
        from systemu.runtime.credentials.at_rest import protect_json
        self._base.mkdir(parents=True, exist_ok=True)
        # S5: encrypt the fallback file at rest via DPAPI on Windows (0o600 is a
        # Windows no-op); chmod stays as the POSIX-secondary control.
        self._file.write_text(protect_json(data), encoding="utf-8")
        try:
            os.chmod(self._file, 0o600)
        except Exception:  # pragma: no cover - non-posix perms
            pass
