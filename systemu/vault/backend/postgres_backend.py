"""postgres_backend.py — psycopg2 dispatch functions for the vault action_audit table.

Used by systemu.vault.backend dispatch layer when vault._storage_backend == 'postgres'.
Mirrors the sqlite_backend.py surface exactly — same function signatures, same
return shapes — but uses psycopg2 and Postgres-appropriate DDL (BIGSERIAL, JSONB).

Connection idiom: open a connection per call, commit, close.  This is safe for
infrequent audit writes and removes any dependency on a connection pool at this layer.
The production SqliteVault (systemu/storage/sqlite/vault.py) uses SQLAlchemy + a pool
for the primary entity store; the action_audit table lives outside that ORM layer and
uses direct psycopg2 here to keep the dispatch modules independent of SQLAlchemy.

Public surface:
    ensure_schema(vault)           — idempotent DDL
    dispatch_append_action_audit(vault, entry)
    dispatch_query_action_audit(vault, *, execution_id, since_ts, user_id)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_ACTION_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS action_audit (
    id            BIGSERIAL PRIMARY KEY,
    ts            TEXT      NOT NULL,
    user_id       TEXT,
    execution_id  TEXT      NOT NULL,
    objective_id  BIGINT    NOT NULL,
    action        TEXT      NOT NULL,
    params_json   JSONB     NOT NULL DEFAULT '{}',
    success       BOOLEAN   NOT NULL,
    error         TEXT
);
"""

_ACTION_AUDIT_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_action_audit_exec_ts
    ON action_audit (execution_id, ts);
CREATE INDEX IF NOT EXISTS ix_action_audit_user_exec
    ON action_audit (user_id, execution_id);
"""


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def ensure_schema(vault) -> None:
    """Idempotent: ensure all v0.9.1 postgres tables exist on the vault's db.

    Uses IF NOT EXISTS so it is safe to call on every startup.
    """
    conn = _connect(vault)
    try:
        cur = conn.cursor()
        cur.execute(_ACTION_AUDIT_DDL)
        cur.execute(_ACTION_AUDIT_INDEXES)
        conn.commit()
        cur.close()
    finally:
        conn.close()


def dispatch_append_action_audit(vault, entry: Dict[str, Any]) -> None:
    """Insert one audit row into the Postgres action_audit table.

    ``entry`` keys: ts, user_id (optional), execution_id, objective_id,
    action, params (dict), success (bool), error (Optional[str]).

    params is stored as JSONB — psycopg2 uses Json() adapter so the dict
    round-trips through the DB as native JSON rather than an escaped string.
    """
    import psycopg2.extras  # noqa: F401 — ensure DictCursor available
    conn = _connect(vault)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO action_audit "
            "(ts, user_id, execution_id, objective_id, action, params_json, success, error) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
            (
                entry["ts"],
                entry.get("user_id"),
                entry["execution_id"],
                int(entry["objective_id"]),
                entry["action"],
                json.dumps(entry.get("params") or {}, separators=(",", ":")),
                bool(entry.get("success")),
                entry.get("error"),
            ),
        )
        conn.commit()
        cur.close()
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

    params_json is returned as a Python dict (psycopg2 decodes JSONB automatically).
    """
    sql = (
        "SELECT ts, user_id, execution_id, objective_id, action, params_json, "
        "success, error FROM action_audit WHERE execution_id = %s"
    )
    args: list = [execution_id]

    if user_id is not None:
        sql += " AND user_id = %s"
        args.append(user_id)
    if since_ts is not None:
        sql += " AND ts >= %s"
        args.append(since_ts)
    sql += " ORDER BY id ASC"

    conn = _connect(vault)
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows: List[Dict[str, Any]] = []
        for r in cur.fetchall():
            # psycopg2 decodes JSONB columns to Python dicts automatically.
            # Guard against TEXT-stored JSON for compatibility with TEXT fallback.
            params = r[5]
            if isinstance(params, str):
                params = json.loads(params) if params else {}
            rows.append({
                "ts":           r[0],
                "user_id":      r[1],
                "execution_id": r[2],
                "objective_id": r[3],
                "action":       r[4],
                "params":       params if params is not None else {},
                "success":      bool(r[6]),
                "error":        r[7],
            })
        cur.close()
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(vault):
    """Open a psycopg2 connection using vault._postgres_url.

    Raises a clear RuntimeError if _postgres_url is not set — passing a
    sqlite:// URL to psycopg2 would produce a confusing connection error.

    In production SqliteVault.__init__ sets _postgres_url when the
    database_url scheme is postgresql:// (v0.9.1 wiring fix).
    """
    import psycopg2

    url = getattr(vault, "_postgres_url", None)
    if not url:
        raise RuntimeError(
            "postgres backend: vault._postgres_url is not set. "
            "Check storage_backend wiring on the vault factory."
        )
    return psycopg2.connect(url)
