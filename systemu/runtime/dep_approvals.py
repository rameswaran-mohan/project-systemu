"""Operator-managed allow-list for tool pip dependencies.

In PROMPT install mode (the local-mode default) the dependency installer
refuses to install a package until the operator has approved it via:

    sharing_on tools deps approve <package>

This module persists those approvals to a JSON file so they survive
daemon restarts.  The file lives at ``<data_dir>/dep_approvals.json``
by default and is plain JSON so an operator can inspect / hand-edit it.

File schema (versioned for forward-compat):

    {
        "version": 1,
        "approved": {
            "python-docx": {
                "approved_at":      "2026-05-13T12:34:56+00:00",
                "approved_by":      "operator",
                "first_seen_tool":  "create_word_doc",
                "first_seen_tool_id": "tool_6e6e62c0"
            },
            ...
        },
        "pending": {
            "Pillow": {
                "first_seen_at":    "2026-05-13T12:35:01+00:00",
                "first_seen_tool":  "create_word_doc",
                "first_seen_tool_id": "tool_6e6e62c0",
                "request_count":    3
            },
            ...
        }
    }

Concurrency: a single ``threading.Lock`` serialises mutations.  The
JSON file is small (operator-scale, not user-scale) and writes are
infrequent, so this is dramatically simpler than file locking.

Why "approval store" and not "table in SQLite":
  * Operator-readable / editable as plain JSON
  * Survives a SQLite migration / vault recreation
  * Lives in ``data/`` next to other runtime state (daemon pid etc.)
"""

from __future__ import annotations

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


_DEFAULT_FILENAME = "dep_approvals.json"


def _publish_dismissal(package: str, *, outcome: str) -> None:
    """Notify the Systemu Chat feed that ``package``'s approval state changed.

    Deferred import so the storage module can be used in contexts where the
    EventBus / NiceGUI surface isn't initialised (CLI, tests).  Any failure
    is silent — operator visibility is best-effort.
    """
    try:
        from systemu.interface.event_bus import EventBus
        EventBus.get().publish_dep_approval_dismissed(package, outcome=outcome)
    except Exception:
        logger.debug("[DepApprovals] could not publish dismissal", exc_info=True)


class DepApprovalStore:
    """Persistent allow-list of approved tool dependencies.

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

    def is_approved(self, package: str) -> bool:
        # v0.3.6: re-read on every check so out-of-process mutations
        # (the CLI / Tools page run in a separate Python interpreter from
        # the daemon) are picked up WITHOUT requiring a daemon restart.
        # Cost: one tiny JSON read per check (~100µs for an operator-scale
        # allow-list).  Cheaper than file-watching plumbing.
        with self._lock:
            self._data = self._load()
            return package in self._data.get("approved", {})

    def approve(
        self,
        package: str,
        *,
        approved_by: str = "operator",
        tool_name:   Optional[str] = None,
        tool_id:     Optional[str] = None,
    ) -> bool:
        """Add a package to the allow-list.  Returns True when newly approved.

        Idempotent: approving an already-approved package returns False and
        leaves the existing record untouched (preserves original timestamp).

        Side effect (v0.3.6): on a newly-recorded approval, publish a
        dismissal event to the Systemu Chat feed so any open approval
        card for this package closes automatically.
        """
        newly_approved = False
        with self._lock:
            # Re-read so we don't clobber a parallel writer.
            self._data = self._load()
            approved: Dict[str, Any] = self._data.setdefault("approved", {})
            pending:  Dict[str, Any] = self._data.setdefault("pending", {})
            if package not in approved:
                existing_pending = pending.pop(package, None) or {}
                approved[package] = {
                    "approved_at":         _now_iso(),
                    "approved_by":         approved_by,
                    "first_seen_tool":     tool_name or existing_pending.get("first_seen_tool"),
                    "first_seen_tool_id":  tool_id   or existing_pending.get("first_seen_tool_id"),
                }
                self._save()
                newly_approved = True
        if newly_approved:
            logger.info("[DepApprovals] approved '%s' (by %s)", package, approved_by)
            _publish_dismissal(package, outcome="approved")
        return newly_approved

    def revoke(self, package: str) -> bool:
        """Remove a package from the allow-list.  Returns True when present.

        Does NOT uninstall the package — that's a separate operator decision.
        Cached "satisfied" state in dependency_installer remains until the
        process restarts.  We accept that staleness because revoke is rare
        and the next process boot will re-validate.
        """
        revoked = False
        with self._lock:
            self._data = self._load()
            approved: Dict[str, Any] = self._data.setdefault("approved", {})
            if package in approved:
                del approved[package]
                self._save()
                revoked = True
        if revoked:
            logger.info("[DepApprovals] revoked '%s'", package)
            _publish_dismissal(package, outcome="revoked")
        return revoked

    def record_pending(
        self,
        package: str,
        *,
        tool_name: Optional[str] = None,
        tool_id:   Optional[str] = None,
    ) -> None:
        """Note that a package was requested but is not yet approved.

        Increments ``request_count`` so the operator can prioritise
        frequently-requested packages.  No-op when the package is already
        approved.
        """
        with self._lock:
            if package in self._data.get("approved", {}):
                return
            pending: Dict[str, Any] = self._data.setdefault("pending", {})
            entry = pending.get(package)
            if entry is None:
                pending[package] = {
                    "first_seen_at":      _now_iso(),
                    "first_seen_tool":    tool_name,
                    "first_seen_tool_id": tool_id,
                    "request_count":      1,
                }
            else:
                entry["request_count"] = int(entry.get("request_count", 0)) + 1
                # Update tool attribution if we didn't have it yet.
                if not entry.get("first_seen_tool") and tool_name:
                    entry["first_seen_tool"] = tool_name
                if not entry.get("first_seen_tool_id") and tool_id:
                    entry["first_seen_tool_id"] = tool_id
            self._save()

    def list_approved(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"package": pkg, **dict(meta)}
                for pkg, meta in sorted(self._data.get("approved", {}).items())
            ]

    def list_pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"package": pkg, **dict(meta)}
                for pkg, meta in sorted(
                    self._data.get("pending", {}).items(),
                    key=lambda kv: -int(kv[1].get("request_count", 0)),
                )
            ]

    # ── Internals ─────────────────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "approved": {}, "pending": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"version": 1, "approved": {}, "pending": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("approval file is not a JSON object")
            data.setdefault("version",  1)
            data.setdefault("approved", {})
            data.setdefault("pending",  {})
            return data
        except Exception:
            logger.exception(
                "[DepApprovals] Could not parse %s — starting with empty store. "
                "The bad file is left in place for operator inspection.",
                self.path,
            )
            return {"version": 1, "approved": {}, "pending": {}}

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
            logger.exception("[DepApprovals] Failed to persist approval store at %s", self.path)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton helpers — convenient for runtime code that doesn't
# want to thread the store through every call site.

_singleton: Optional[DepApprovalStore] = None
_singleton_lock = threading.Lock()


def init_default_store(data_dir: Path) -> DepApprovalStore:
    """Initialise the process-wide singleton at ``<data_dir>/dep_approvals.json``.

    Idempotent: calling twice with the same ``data_dir`` returns the existing
    instance.  Calling with a different ``data_dir`` rebuilds (used in tests).
    """
    global _singleton
    target = Path(data_dir) / _DEFAULT_FILENAME
    with _singleton_lock:
        if _singleton is None or _singleton.path != target:
            _singleton = DepApprovalStore(target)
        return _singleton


def get_default_store() -> Optional[DepApprovalStore]:
    """Return the process-wide singleton, or None if not initialised yet."""
    with _singleton_lock:
        return _singleton


def reset_default_store_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None


# ─────────────────────────────────────────────────────────────────────────────
# v0.6.8-e: database-backed runtime approval workflow for the dashboard
# recovery panel.  Separate from the JSON-file DepApprovalStore above —
# this writes to the ``tool_dep_approvals`` SQLAlchemy table seeded by
# v0.6.8-d.  The two stores coexist:
#   * DepApprovalStore: PROMPT mode allow-list (local mode default)
#   * ToolDepApproval table: dashboard-driven approvals that the docker
#     image bake pipeline consumes via ``tools/requirements-tools.txt``.

def approve_and_install(*, tool_id: str, package: str, source: str = "dashboard") -> None:
    """Persist operator approval, pip-install the package, re-run dry-run.

    Storage-aware (v0.8.13): file mode persists to the JSON DepApprovalStore
    (the store the local PROMPT installer actually consults); docker/sqlite
    mode persists to the tool_dep_approvals table. Both then pip-install + dry-run.

    Called from the dashboard recovery panel (/recover) and the dep_approval
    notification when an operator clicks "Install <pkg>".  Three steps:
      1. Persist the approval (JSON store in file mode; ToolDepApproval row
         in sqlite/docker mode).
      2. Run ``pip install <pkg>`` into the current interpreter.
      3. Re-run dry-run for ``tool_id`` so the tool's status updates.

    Raises:
        RuntimeError: when ``pip install`` exits non-zero.  The approval
                      is still persisted (operator intent is captured);
                      a subsequent docker rebuild will retry the install.
    """
    import os
    from pathlib import Path

    if os.environ.get("SYSTEMU_DATABASE_URL"):
        approval = _make_approval(package=package, source=source)
        _persist_approval(approval)
    else:
        # file mode — write the JSON store the installer reads
        store = DepApprovalStore(Path("data") / "dep_approvals.json")
        store.approve(package, approved_by=source, tool_id=tool_id)

    rc = _run_pip_install(package)
    if rc != 0:
        raise RuntimeError(f"pip install {package} failed (rc={rc})")
    _rerun_dry_run(tool_id)


def is_allowlisted(package: str) -> bool:
    """True iff ``package`` has a row in the ``tool_dep_approvals`` table."""
    return package in _load_allowlist()


def _dep_engine():
    """Return a SQLAlchemy engine for the dep-approvals store, or ``None`` if the
    configured database is unreachable.

    ``SYSTEMU_DATABASE_URL`` may be unset, or point at Postgres while the driver
    (psycopg2) isn't installed — SQLAlchemy then raises ``ModuleNotFoundError``
    on first connect. Rather than crash the caller (a tool's dependency check
    via ``is_allowlisted``, or the /tools banner), we degrade: callers treat the
    allowlist as empty and skip persistence. Logged once at WARNING.
    """
    url = os.environ.get("SYSTEMU_DATABASE_URL", "") or ""
    if not url:
        return None
    try:
        from sqlalchemy import create_engine
        engine = create_engine(url)
        # Force driver import / connectivity now so a raw ModuleNotFoundError
        # can't surface from a query site deep in the run.
        with engine.connect():
            pass
        return engine
    except Exception as exc:  # noqa: BLE001 — any driver/connectivity failure degrades
        logger.warning(
            "[dep_approvals] database unavailable (%s: %s) — degrading: dependency "
            "allowlist treated as empty, approvals not persisted.",
            (url.split("://", 1)[0] or "?"), exc.__class__.__name__,
        )
        return None


def list_unbaked_approvals():
    """Return ToolDepApproval rows that are runtime-approved but not yet
    baked into the docker image (``baked_in_image=False``).

    Used by the /tools page banner (v0.6.8-e) to remind operators to run
    ``docker compose build`` so freshly-approved deps survive the next
    container rebuild. Degrades to ``[]`` when the database is unreachable.
    """
    from sqlalchemy.orm import Session
    from systemu.storage.sqlite.models import ToolDepApproval
    engine = _dep_engine()
    if engine is None:
        return []
    with Session(engine) as s:
        rows = s.query(ToolDepApproval).filter_by(baked_in_image=False).all()
        for r in rows:
            s.expunge(r)
        return rows


def _make_approval(*, package: str, source: str):
    import uuid
    from systemu.storage.sqlite.models import ToolDepApproval
    return ToolDepApproval(
        id=f"dep_{uuid.uuid4().hex[:8]}",
        package_name=package,
        approved_by=source,
        source=source,
        baked_in_image=False,
    )


def _persist_approval(approval) -> None:
    from sqlalchemy.orm import Session
    engine = _dep_engine()
    if engine is None:
        logger.warning("[dep_approvals] skipping persist of %s — database unavailable.",
                       getattr(approval, "package_name", "?"))
        return
    with Session(engine) as s:
        s.add(approval)
        s.commit()


def _load_allowlist() -> set:
    from sqlalchemy.orm import Session
    from systemu.storage.sqlite.models import ToolDepApproval
    engine = _dep_engine()
    if engine is None:
        return set()
    with Session(engine) as s:
        return {r.package_name for r in s.query(ToolDepApproval).all()}


def _run_pip_install(package: str) -> int:
    """Synchronous ``pip install --no-cache-dir <package>`` invocation.

    Returns the subprocess exit code (0 == success).  Used by
    ``approve_and_install`` — distinct from the in-module v0.3.x
    DepApprovalStore flow which delegates to dependency_installer.py.
    """
    import subprocess
    import sys
    return subprocess.call(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", package]
    )


def _rerun_dry_run(tool_id: str) -> None:
    """Best-effort post-install dry-run; never raises."""
    try:
        from systemu.scheduler.jobs import dry_run_one_tool
    except ImportError:
        return
    try:
        dry_run_one_tool(tool_id)
    except Exception:
        logger.warning(
            "post-install dry-run for %s failed", tool_id, exc_info=True,
        )
