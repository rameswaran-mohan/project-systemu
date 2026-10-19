"""v0.8.18 — secure credential storage (OS keychain via keyring, 0600-file fallback)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable, List, Optional

# THE ONE MINT for "which directory is the operating vault" (see that module's
# docstring). Module scope is safe here: `vault_root` imports only the standard
# library at import time and `systemu.runtime.__init__` is a bare docstring, so
# this adds neither a cycle nor a hot-path cost to the resolver that constructs
# a store per harness credential lookup.
from systemu.runtime.vault_root import resolve_vault_root

logger = logging.getLogger(__name__)
_SERVICE = "systemu"

#: Serializes every read-modify-write of the NAMES registry inside this process.
#: Until R-B1 its only writers were `set`/`delete`, driven by an operator click.
#: The names backfill adds a BACKGROUND writer (the daemon's table health tick),
#: and unlocked, a backfill that loaded before a `delete` and wrote after it would
#: put the deleted name back -- a removed credential reading as present. Not
#: reentrant; nothing under it may call `_record_name` / `_forget_name` again.
_NAMES_LOCK = threading.Lock()


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
        # The base is ONE absolute path minted from the operating home, never a
        # second reading of SYSTEMU_VAULT_DIR. The old default was the RELATIVE
        # string "systemu/vault", so `.credentials.json` -- a secret at rest --
        # landed wherever the reading process happened to be standing. That was
        # safe only BY INHERITANCE (the daemon pins an absolute value into its
        # child's env); nothing here enforced it, and the callers that pass no
        # base_dir (CredentialResolver, the dashboard settings page) do not all
        # descend from that daemon.
        #
        # The `refused` bit is deliberately NOT consulted here, exactly as in
        # `sharing_on.config._resolve_vault_dir`: this is a consumer of the
        # root, not the boundary that decides to boot. The refusal is a VALUE
        # adjudicated at the boot boundary (`start_daemon` /
        # `resolve_child_vault_dir`); re-raising it here would be a raise with
        # catching frames above it, which DEC-32 says is no fence at all.
        #
        # `or` short-circuits, so callers that already hold an absolute
        # `vault.root` do not pay for the mint call.
        self._base = Path(base_dir or resolve_vault_root().root)
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
        # tmp + os.replace (R-B1): the OnTheTable health check now READS this file
        # every tick to decide whether a credential was removed, and a plain
        # rewrite would let it read a half-written registry.
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._base),
                                       prefix=".credential_names.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(sorted(set(names))))
                os.replace(tmp, str(self._names_file))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:  # the name registry must never break credential I/O
            logger.debug("[Credentials] name registry write failed: %s", exc)

    def _record_name(self, key: str) -> None:
        with _NAMES_LOCK:
            names = self._read_names()
            if key not in names:
                self._write_names(names + [key])

    def _forget_name(self, key: str) -> None:
        with _NAMES_LOCK:
            names = self._read_names()
            if key in names:
                self._write_names([n for n in names if n != key])

    def _in_keyring(self, key: str) -> bool:
        """True iff the OS keyring holds a value for ``key``. The value is dropped
        in this frame and never logged."""
        if self._keyring is None:
            return False
        try:
            value = self._keyring.get_password(_SERVICE, key)
        except Exception:
            return False
        present = bool(value)
        value = None  # noqa: F841 - drop the plaintext reference promptly
        return present

    def backfill_names(self, candidates: Iterable[str]) -> Optional[List[str]]:
        """R-B1: record the NAME of every stored credential the registry is missing.

        The registry only learns a name through ``set``, so a credential written
        before the registry existed (or by anything else) is readable through
        ``get`` yet invisible to the OnTheTable projection. Two sources are
        checked: the ``candidates`` (the caller passes the keys tools DECLARE --
        the keyring cannot enumerate itself, so a declaration is the only way to
        know which names to ask it about) and every key of the at-rest file, which
        CAN be enumerated. A name is recorded only when a value is actually
        stored under it -- never for a credential that does not exist.

        IDEMPOTENT: a name already registered is skipped without reading anything,
        so a second call records nothing and leaves the file byte-identical.
        Returns the names it recorded, or ``None`` when it refused because the
        registry could not be read (writing would replace every name the
        unreadable file holds). Names only; no value is ever written or logged.
        """
        with _NAMES_LOCK:
            registered = self.read_names_witnessed()
            if registered is None:
                return None
            have = set(registered)
            try:
                file_keys = {k for k in self._read_file() if type(k) is str and k}
            except Exception:
                file_keys = set()
            wanted = {k for k in candidates if type(k) is str and k} | file_keys
            found = sorted(k for k in wanted - have
                           if k in file_keys or self._in_keyring(k))
            if found:
                self._write_names(sorted(have) + found)
            return found

    def list_names(self) -> list:
        """The registered credential NAMES (no values) — the OnTheTable projection
        surface (keyring cannot enumerate its own entries)."""
        return sorted(self._read_names())

    def read_names_witnessed(self) -> Optional[list]:
        """The registered NAMES, or ``None`` when the registry cannot be WITNESSED.

        ``list_names`` answers ``[]`` for an unreadable registry, which is right for
        a display and wrong for the two R-B1 callers that must tell "nothing is
        registered" from "the registry could not be read": the OnTheTable health
        check (an unreadable registry would otherwise break every credential card
        as "credential removed" while the secrets sit untouched in the keyring) and
        the names backfill (writing through an unreadable registry would replace
        every name it holds). A registry that is genuinely ABSENT under an existing
        base directory is a witnessed empty answer; anything else that is not a
        JSON list is ``None``. Never raises; never reads a secret.
        """
        try:
            raw = self._names_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            try:
                return [] if self._base.is_dir() else None
            except Exception:
                return None
        except Exception:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if type(data) is not list:
            return None
        return sorted(n for n in data if type(n) is str)

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
