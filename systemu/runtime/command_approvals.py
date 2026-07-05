"""Operator-managed allow-list for destructive shell commands (v0.9.32, D-2).

Mirrors DepApprovalStore (systemu/runtime/dep_approvals.py): a small JSON file
the operator can inspect/hand-edit, re-read on every check so an out-of-process
"Always allow" is seen without a daemon restart. Keyed on an EXACT normalized
command signature = sha1(whitespace-collapsed command + "\x00" + cwd).

In a per-command approval gate the operator confirms a harmful shell command
before it runs.  "Always allow" persists that exact (command, cwd) so future
identical runs skip the gate.  Persisted here so it survives daemon restarts and
is operator-readable/editable as plain JSON.

File schema (versioned for forward-compat):

    {
        "version": 1,
        "approved": {
            "<sha1-sig>": {
                "approved_at": "2026-06-15T12:34:56+00:00",
                "approved_by": "operator",
                "command":     "rm -rf build",
                "cwd":         "/proj"
            },
            ...
        },
        "pending": {
            "<sha1-sig>": {
                "first_seen_at": "2026-06-15T12:35:01+00:00",
                "command":       "rm -rf build",
                "cwd":           "/proj",
                "request_count": 3
            },
            ...
        }
    }

Concurrency: a single ``threading.Lock`` serialises mutations.  The JSON file is
small (operator-scale) and writes are infrequent, so this is dramatically
simpler than file locking — same trade-off DepApprovalStore makes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


_DEFAULT_FILENAME = "command_approvals.json"


def command_signature(command: str, *, cwd: str = "") -> str:
    """Exact normalized signature: sha1 of whitespace-collapsed command + cwd.

    Whitespace runs collapse to a single space and ends are stripped so
    cosmetic reformatting does not invalidate an "Always allow" (``ls  -la`` ==
    ``ls -la``). cwd is part of the key — the same command in a different
    directory is a NEW decision. This is EXACT-match, not fuzzy: a different
    command never collides.
    """
    norm_cmd = " ".join((command or "").split())
    norm_cwd = (cwd or "").strip()
    raw = f"{norm_cmd}\x00{norm_cwd}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def mcp_signature(server: str, tool: str) -> str:
    """Always-allow signature for an MCP (server, tool) pair.

    The server is whitespace-stripped and trailing-slash-normalized (matching
    connections._path / is_tool_enabled normalization) so a cosmetic trailing
    "/" does not invalidate a persisted Always-allow. EXACT-match: a different
    server or tool never collides.
    """
    norm_server = (server or "").strip().rstrip("/")
    norm_tool = (tool or "").strip()
    raw = f"mcp\x00{norm_server}\x00{norm_tool}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def mcp_session_key(server: str, tool: str, session_id: str) -> str:
    """Session-scoped trust key for an MCP (server, tool) within ONE run.

    session_id is part of the hash, so a trust grant CANNOT leak across runs:
    a new run has a new session_id → a fresh, untrusted key.
    """
    norm_server = (server or "").strip().rstrip("/")
    norm_tool = (tool or "").strip()
    norm_sess = (session_id or "").strip()
    raw = f"mcp-session\x00{norm_server}\x00{norm_tool}\x00{norm_sess}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def tool_signature(name: str, body_hash: str, effect_tags, *, host_class: str = "") -> str:
    """IMPL-1 approval key for a forged/registry tool (S1b live action gate).

    Order-insensitive over effect_tags (sorted before hashing); a changed
    body_hash, tag set, or host_class invalidates the blessing. host_class
    defaults to "" so non-network tools get a stable 3-tuple signature.
    Disjoint from mcp_signature via the "tool" domain prefix. body_hash and
    host_class are computed elsewhere and passed in as strings — this
    function does no filesystem/host work.
    """
    norm_name = (name or "").strip()
    tags = "\x00".join(sorted(str(t).strip() for t in (effect_tags or [])))
    raw = f"tool\x00{norm_name}\x00{body_hash}\x00{tags}\x00{host_class}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


class CommandApprovalStore:
    """Persistent allow-list of operator-approved command signatures.

    Args:
        path: Path to the JSON file.  Parent directories are created on
              first write.  When the file does not exist (or is unreadable)
              the store starts empty — never raises in the constructor so
              the daemon can boot in a fresh checkout.
    """

    def __init__(self, path: Path):
        self.path: Path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def is_approved(self, sig: str) -> bool:
        # Re-read on every check so out-of-process mutations (the dashboard /
        # CLI run in a separate Python interpreter from the daemon) are picked
        # up WITHOUT a daemon restart. Cost: one tiny JSON read per check —
        # cheaper than file-watching plumbing. (Mirrors DepApprovalStore.)
        with self._lock:
            self._data = self._load()
            return sig in self._data.get("approved", {})

    def is_session_trusted(self, session_key: str) -> bool:
        """Return True iff this exact session-trust key is on record.

        Re-reads the file on every check (same out-of-process freshness
        contract as is_approved) so a dashboard-side "Trust for session"
        click is seen by the daemon without a restart.
        """
        with self._lock:
            self._data = self._load()
            return session_key in self._data.get("session_trusted", {})

    def trust_session(
        self,
        session_key: str,
        *,
        server: str = "",
        tool: str = "",
        session_id: str = "",
        approved_by: str = "operator",
    ) -> bool:
        """Record session-scoped trust for an MCP (server, tool, session).

        Returns True when newly trusted. Idempotent: re-trusting an existing
        key returns False and leaves the record untouched.
        """
        newly = False
        with self._lock:
            self._data = self._load()
            trusted: Dict[str, Any] = self._data.setdefault("session_trusted", {})
            if session_key not in trusted:
                trusted[session_key] = {
                    "trusted_at": _now_iso(),
                    "trusted_by": approved_by,
                    "server":     server,
                    "tool":       tool,
                    "session_id": session_id,
                }
                self._save()
                newly = True
        if newly:
            logger.info("[CommandApprovals] session-trusted %s (%s:%s, run %s)",
                        session_key, server, tool, session_id)
        return newly

    def mark_resume_approved(self, sig: str) -> None:
        """v0.9.52: record a SINGLE-USE approval for a command signature.

        Set by the command-gate RESUME path so the resumed run honors the
        operator's "Approve once" exactly once, across the park→resume boundary.
        Unlike :meth:`approve` this is NOT a standing allow-list entry — it's a
        one-shot bridge, consumed on the first :meth:`consume_resume_approved`.
        """
        with self._lock:
            self._data = self._load()
            pend: Dict[str, Any] = self._data.setdefault("resume_pending", {})
            pend[sig] = {"marked_at": _now_iso()}
            self._save()

    def consume_resume_approved(self, sig: str) -> bool:
        """Return True (and REMOVE the entry) iff a one-shot resume-approval is on
        record for ``sig``. Single-use — a second check returns False."""
        with self._lock:
            self._data = self._load()
            pend = self._data.get("resume_pending") or {}
            if sig in pend:
                del pend[sig]
                self._save()
                return True
        return False

    def approve(
        self,
        sig: str,
        *,
        command: str = "",
        cwd: str = "",
        approved_by: str = "operator",
    ) -> bool:
        """Record an "Always allow" for an exact command signature.

        Returns True when newly approved.  Idempotent: approving an
        already-approved signature returns False and leaves the existing
        record (and its timestamp) untouched.
        """
        newly = False
        with self._lock:
            # Re-read so we don't clobber a parallel writer.
            self._data = self._load()
            approved: Dict[str, Any] = self._data.setdefault("approved", {})
            pending: Dict[str, Any] = self._data.setdefault("pending", {})
            if sig not in approved:
                existing_pending = pending.pop(sig, None) or {}
                approved[sig] = {
                    "approved_at": _now_iso(),
                    "approved_by": approved_by,
                    "command":     command or existing_pending.get("command", ""),
                    "cwd":         cwd or existing_pending.get("cwd", ""),
                }
                self._save()
                newly = True
        if newly:
            logger.info("[CommandApprovals] approved %s (cmd=%r, by %s)",
                        sig, command, approved_by)
        return newly

    def revoke(self, sig: str) -> bool:
        """Remove a signature from the allow-list.  Returns True when present."""
        revoked = False
        with self._lock:
            self._data = self._load()
            approved: Dict[str, Any] = self._data.setdefault("approved", {})
            if sig in approved:
                del approved[sig]
                self._save()
                revoked = True
        if revoked:
            logger.info("[CommandApprovals] revoked %s", sig)
        return revoked

    def record_pending(
        self,
        sig: str,
        *,
        command: str = "",
        cwd: str = "",
    ) -> None:
        """Note that a command was requested but is not yet approved.

        Increments ``request_count`` so the operator can prioritise
        frequently-requested commands.  No-op when the signature is already
        approved.  (Mirrors DepApprovalStore.record_pending.)
        """
        with self._lock:
            if sig in self._data.get("approved", {}):
                return
            pending: Dict[str, Any] = self._data.setdefault("pending", {})
            entry = pending.get(sig)
            if entry is None:
                pending[sig] = {
                    "first_seen_at": _now_iso(),
                    "command":       command,
                    "cwd":           cwd,
                    "request_count": 1,
                }
            else:
                entry["request_count"] = int(entry.get("request_count", 0)) + 1
                if not entry.get("command") and command:
                    entry["command"] = command
                if not entry.get("cwd") and cwd:
                    entry["cwd"] = cwd
            self._save()

    def list_approved(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"signature": s, **dict(meta)}
                for s, meta in sorted(self._data.get("approved", {}).items())
            ]

    def list_pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"signature": s, **dict(meta)}
                for s, meta in sorted(
                    self._data.get("pending", {}).items(),
                    key=lambda kv: -int(kv[1].get("request_count", 0)),
                )
            ]

    # ── Internals ─────────────────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "approved": {}, "pending": {}, "session_trusted": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"version": 1, "approved": {}, "pending": {}, "session_trusted": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("approval file is not a JSON object")
            data.setdefault("version",  1)
            data.setdefault("approved", {})
            data.setdefault("pending",  {})
            data.setdefault("session_trusted", {})
            return data
        except Exception:
            logger.exception(
                "[CommandApprovals] Could not parse %s — starting with empty store. "
                "The bad file is left in place for operator inspection.",
                self.path,
            )
            return {"version": 1, "approved": {}, "pending": {}, "session_trusted": {}}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[CommandApprovals] Failed to persist store at %s",
                             self.path)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton helpers — convenient for runtime code that doesn't
# want to thread the store through every call site. (Mirrors dep_approvals.)

_singleton: Optional[CommandApprovalStore] = None
_singleton_lock = threading.Lock()


def init_default_store(data_dir: Path) -> CommandApprovalStore:
    """Initialise the process-wide singleton at ``<data_dir>/command_approvals.json``.

    Idempotent: calling twice with the same ``data_dir`` returns the existing
    instance.  Calling with a different ``data_dir`` rebuilds (used in tests).
    """
    global _singleton
    target = Path(data_dir) / _DEFAULT_FILENAME
    with _singleton_lock:
        if _singleton is None or _singleton.path != target:
            _singleton = CommandApprovalStore(target)
        return _singleton


def get_default_store() -> Optional[CommandApprovalStore]:
    """Return the process-wide singleton, or None if not initialised yet."""
    with _singleton_lock:
        return _singleton


def reset_default_store_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
