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


class CredentialStore:
    """keyring-backed secret store; falls back to a 0600 JSON file under the vault dir."""

    def __init__(self, base_dir=None):
        self._base = Path(base_dir or os.getenv("SYSTEMU_VAULT_DIR", "systemu/vault"))
        self._keyring = self._init_keyring()

    def _init_keyring(self):
        try:
            import keyring
            keyring.get_keyring()
            return keyring
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("[Credentials] keyring unavailable (%s); using 0600 file fallback", exc)
            return None

    @property
    def _file(self) -> Path:
        return self._base / ".credentials.json"

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
        data = self._read_file()
        data[key] = value
        self._write_file(data)
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

    def status(self, key: str) -> dict:
        v = self.get(key)
        return {"present": v is not None,
                "last4": (v[-4:] if v else None),
                "backend": "keyring" if self._keyring is not None else "file"}

    def _read_file(self) -> dict:
        try:
            if self._file.exists():
                return json.loads(self._file.read_text(encoding="utf-8"))
        except Exception as exc:
            # A corrupt/unreadable store must not masquerade as "no credentials"
            # — that silently hides real secrets and may trigger re-provisioning.
            logger.warning("[Credentials] failed to read credential file %s: %s",
                           self._file, exc)
        return {}

    def _write_file(self, data: dict) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(self._file, 0o600)
        except Exception:  # pragma: no cover - non-posix perms
            pass
