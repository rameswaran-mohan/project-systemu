"""sqlite_backend.py — lightweight sqlite3 dispatch functions for the vault action_audit table.

Used by systemu.vault.backend dispatch layer when vault._storage_backend == 'sqlite'.
Intentionally avoids SQLAlchemy — this is a thin direct sqlite3 module so it has
zero extra dependencies beyond the stdlib and works identically on every platform.

Public surface:
    ensure_schema(vault)           — idempotent DDL (called once at vault init time)
    dispatch_append_action_audit(vault, entry) — INSERT one row
    dispatch_query_action_audit(vault, *, execution_id, since_ts, user_id) — SELECT rows
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_ACTION_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS action_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    user_id       TEXT,
    execution_id  TEXT    NOT NULL,
    objective_id  INTEGER NOT NULL,
    action        TEXT    NOT NULL,
    params_json   TEXT    NOT NULL,
    success       INTEGER NOT NULL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_action_audit_exec_ts
    ON action_audit (execution_id, ts);
CREATE INDEX IF NOT EXISTS ix_action_audit_user_exec
    ON action_audit (user_id, execution_id);
"""


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def ensure_schema(vault) -> None:
    """Idempotent: ensure all v0.9.1 sqlite tables exist on the vault's db.

    Called once during vault fixture setup (test) or vault factory init
    (production). Safe to call multiple times — CREATE TABLE IF NOT EXISTS
    and CREATE INDEX IF NOT EXISTS are both idempotent.
    """
    conn = _connect(vault)
    try:
        conn.executescript(_ACTION_AUDIT_DDL)
        conn.commit()
    finally:
        conn.close()


def dispatch_append_action_audit(vault, entry: Dict[str, Any]) -> None:
    """Insert one audit row.

    Called from vault.append_action_audit when storage_backend == 'sqlite'.

    ``entry`` keys: ts, user_id (optional), execution_id, objective_id,
    action, params (dict), success (bool), error (Optional[str]).
    """
    conn = _connect(vault)
    try:
        conn.execute(
            "INSERT INTO action_audit "
            "(ts, user_id, execution_id, objective_id, action, params_json, success, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["ts"],
                entry.get("user_id"),
                entry["execution_id"],
                int(entry["objective_id"]),
                entry["action"],
                json.dumps(entry.get("params") or {}, separators=(",", ":")),
                1 if entry.get("success") else 0,
                entry.get("error"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def dispatch_query_action_audit(
    vault,
    *,
    execution_id: str,
    since_ts: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return matching audit rows ordered by insertion order (id ASC).

    Filters are AND-combined:
      execution_id  — required, equality match
      user_id       — optional, equality match
      since_ts      — optional, inclusive lower bound on ts (ISO string compare)
    """
    sql = (
        "SELECT ts, user_id, execution_id, objective_id, action, params_json, "
        "success, error FROM action_audit WHERE execution_id = ?"
    )
    args: list = [execution_id]

    if user_id is not None:
        sql += " AND user_id = ?"
        args.append(user_id)
    if since_ts is not None:
        sql += " AND ts >= ?"
        args.append(since_ts)
    sql += " ORDER BY id ASC"

    conn = _connect(vault)
    try:
        cur = conn.execute(sql, args)
        rows: List[Dict[str, Any]] = []
        for r in cur.fetchall():
            rows.append({
                "ts":           r[0],
                "user_id":      r[1],
                "execution_id": r[2],
                "objective_id": r[3],
                "action":       r[4],
                "params":       json.loads(r[5]) if r[5] else {},
                "success":      bool(r[6]),
                "error":        r[7],
            })
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(vault) -> sqlite3.Connection:
    """Resolve sqlite3 connection from vault._sqlite_url.

    Accepts URLs in the form ``sqlite:///absolute/path/to/db`` (three slashes
    for absolute paths, as produced by the test fixture and the vault factory).
    Strips the ``sqlite:///`` prefix to get the filesystem path.
    """
    url: str = vault._sqlite_url
    path = url.replace("sqlite:///", "", 1)
    return sqlite3.connect(path)
