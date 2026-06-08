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
    dispatch_append_session_summary(vault, summary)
    dispatch_query_session_summaries(vault, *, user_id, status, since_ts, limit)
    dispatch_search_session_summaries(vault, *, query, user_id, limit)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# psycopg2 is an optional dependency: the postgres backend is only exercised when
# vault._storage_backend == 'postgres'.  Import it defensively so that merely
# importing this module (e.g. via the systemu.vault.backend dispatch layer on a
# sqlite/file deployment) never raises ModuleNotFoundError.  Functions that
# actually need psycopg2 raise a clear, catchable RuntimeError when it is None,
# which the dispatch layer turns into a graceful sqlite fallback.
try:
    import psycopg2  # noqa: F401 — re-exported for availability checks
except Exception:  # pragma: no cover - exercised only when psycopg2 is absent
    psycopg2 = None


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

# v0.9.2: session_summaries table with tsvector for full-text search.
# DDL is split into individual statements because psycopg2 has no executescript
# equivalent — each statement is executed via a separate cur.execute() call.
_SESSION_SUMMARIES_DDL_STATEMENTS = [
    # Main table
    """
CREATE TABLE IF NOT EXISTS session_summaries (
    id              TEXT      PRIMARY KEY,
    session_id      TEXT      NOT NULL,
    execution_id    TEXT,
    user_id         TEXT,
    started_at      TIMESTAMP NOT NULL,
    completed_at    TIMESTAMP NOT NULL,
    status          TEXT      NOT NULL,
    intent          TEXT      NOT NULL,
    outcome_summary TEXT      NOT NULL,
    key_facts_json  JSONB     NOT NULL DEFAULT '[]',
    files_json      JSONB     NOT NULL DEFAULT '[]',
    tags_json       JSONB     NOT NULL DEFAULT '[]',
    raw_chat_id     TEXT,
    tsv             tsvector
)
""",
    # Indexes
    """
CREATE INDEX IF NOT EXISTS ix_session_summaries_user_completed
    ON session_summaries (user_id, completed_at)
""",
    """
CREATE INDEX IF NOT EXISTS ix_session_summaries_status
    ON session_summaries (status)
""",
    """
CREATE INDEX IF NOT EXISTS session_summaries_tsv_idx
    ON session_summaries USING GIN (tsv)
""",
    # tsvector trigger function
    """
CREATE OR REPLACE FUNCTION session_summaries_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('english',
        coalesce(NEW.intent, '') || ' ' ||
        coalesce(NEW.outcome_summary, '') || ' ' ||
        coalesce(NEW.tags_json::text, '')
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
""",
    # Drop existing trigger (idempotent via DROP IF EXISTS)
    "DROP TRIGGER IF EXISTS session_summaries_tsv_upd ON session_summaries",
    # Recreate trigger
    """
CREATE TRIGGER session_summaries_tsv_upd
    BEFORE INSERT OR UPDATE ON session_summaries
    FOR EACH ROW EXECUTE FUNCTION session_summaries_tsv_trigger()
""",
]


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def ensure_schema(vault) -> None:
    """Idempotent: ensure all postgres tables exist on the vault's db.

    Runs both v0.9.1 (action_audit) and v0.9.2 (session_summaries + tsvector)
    DDL. Uses IF NOT EXISTS so it is safe to call on every startup.
    Each statement is executed individually — psycopg2 has no executescript.
    """
    conn = _connect(vault)
    try:
        cur = conn.cursor()
        cur.execute(_ACTION_AUDIT_DDL)
        cur.execute(_ACTION_AUDIT_INDEXES)
        for stmt in _SESSION_SUMMARIES_DDL_STATEMENTS:
            cur.execute(stmt)
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
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not available")
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


def dispatch_append_session_summary(vault, summary) -> None:
    """Insert one session_summary row into the Postgres session_summaries table.

    Called from vault.append_session_summary when storage_backend == 'postgres'.
    ``summary`` is a SessionSummary Pydantic model instance.

    JSONB columns (key_facts_json, files_json, tags_json) are cast via
    ``%s::jsonb`` — psycopg2 does not auto-cast Python lists to JSONB.
    The tsvector column is populated automatically by the
    session_summaries_tsv_upd BEFORE INSERT trigger.
    """
    conn = _connect(vault)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO session_summaries "
            "(id, session_id, execution_id, user_id, started_at, completed_at, "
            "status, intent, outcome_summary, key_facts_json, files_json, tags_json, raw_chat_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)",
            (
                summary.id,
                summary.session_id,
                summary.execution_id,
                summary.user_id,
                summary.started_at.isoformat(),
                summary.completed_at.isoformat(),
                summary.status,
                summary.intent,
                summary.outcome_summary,
                json.dumps(summary.key_facts_learned or []),
                json.dumps(summary.files_produced or []),
                json.dumps(summary.tags or []),
                summary.raw_chat_id,
            ),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def dispatch_query_session_summaries(vault, *, user_id=None, status=None,
                                      since_ts=None, limit=None):
    """Return matching session_summary rows ordered by id ASC.

    Filters are AND-combined:
      user_id   — optional, equality match
      status    — optional, equality match
      since_ts  — optional, inclusive lower bound on completed_at (datetime)

    JSONB columns (key_facts_json, files_json, tags_json) are returned as
    Python lists by psycopg2 — no json.loads() needed.
    """
    from systemu.core.models import SessionSummary
    from datetime import datetime as _dt

    sql = (
        "SELECT id, session_id, execution_id, user_id, started_at, completed_at, "
        "status, intent, outcome_summary, key_facts_json, files_json, tags_json, "
        "raw_chat_id FROM session_summaries WHERE 1=1"
    )
    args: list = []

    if user_id is not None:
        sql += " AND user_id = %s"
        args.append(user_id)
    if status is not None:
        sql += " AND status = %s"
        args.append(status)
    if since_ts is not None:
        sql += " AND completed_at >= %s"
        args.append(since_ts.isoformat())
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    conn = _connect(vault)
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    out = []
    for r in rows:
        # psycopg2 decodes JSONB to Python objects automatically.
        # Guard against TEXT-stored JSON for compatibility.
        def _list(v):
            if isinstance(v, str):
                return json.loads(v) if v else []
            return v if v is not None else []

        out.append(SessionSummary(
            id=r[0], session_id=r[1], execution_id=r[2], user_id=r[3],
            started_at=r[4] if isinstance(r[4], _dt) else _dt.fromisoformat(r[4]),
            completed_at=r[5] if isinstance(r[5], _dt) else _dt.fromisoformat(r[5]),
            status=r[6], intent=r[7], outcome_summary=r[8],
            key_facts_learned=_list(r[9]),
            files_produced=_list(r[10]),
            tags=_list(r[11]),
            raw_chat_id=r[12],
        ))
    return out


def dispatch_search_session_summaries(vault, *, query, user_id=None, limit=5):
    """tsvector keyword search over intent, outcome_summary, and tags_json.

    Uses ``plainto_tsquery('english', ...)`` with a GIN index on the ``tsv``
    column (populated by the session_summaries_tsv_upd trigger).
    Results are ordered by ts_rank descending (most relevant first).
    Returns [] for empty/blank queries.
    """
    from systemu.core.models import SessionSummary
    from datetime import datetime as _dt

    if not query or not query.strip():
        return []

    sql = (
        "SELECT s.id, s.session_id, s.execution_id, s.user_id, s.started_at, "
        "s.completed_at, s.status, s.intent, s.outcome_summary, "
        "s.key_facts_json, s.files_json, s.tags_json, s.raw_chat_id "
        "FROM session_summaries s "
        "WHERE s.tsv @@ plainto_tsquery('english', %s)"
    )
    args: list = [query.strip()]

    if user_id is not None:
        sql += " AND s.user_id = %s"
        args.append(user_id)

    # ts_rank needs the tsquery twice — once for ordering
    sql += " ORDER BY ts_rank(s.tsv, plainto_tsquery('english', %s)) DESC"
    args.append(query.strip())
    sql += f" LIMIT {int(limit)}"

    conn = _connect(vault)
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    out = []
    for r in rows:
        def _list(v):
            if isinstance(v, str):
                return json.loads(v) if v else []
            return v if v is not None else []

        out.append(SessionSummary(
            id=r[0], session_id=r[1], execution_id=r[2], user_id=r[3],
            started_at=r[4] if isinstance(r[4], _dt) else _dt.fromisoformat(r[4]),
            completed_at=r[5] if isinstance(r[5], _dt) else _dt.fromisoformat(r[5]),
            status=r[6], intent=r[7], outcome_summary=r[8],
            key_facts_learned=_list(r[9]),
            files_produced=_list(r[10]),
            tags=_list(r[11]),
            raw_chat_id=r[12],
        ))
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(vault):
    """Open a psycopg2 connection using vault._postgres_url.

    Raises a clear RuntimeError if _postgres_url is not set — passing a
    sqlite:// URL to psycopg2 would produce a confusing connection error.

    In production SqliteVault.__init__ sets _postgres_url when the
    database_url scheme is postgresql:// (v0.9.1 wiring fix).

    Raises RuntimeError if psycopg2 is not installed — callers (the dispatch
    layer) catch this and degrade to the sqlite backend rather than crashing.
    """
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not available")

    url = getattr(vault, "_postgres_url", None)
    if not url:
        raise RuntimeError(
            "postgres backend: vault._postgres_url is not set. "
            "Check storage_backend wiring on the vault factory."
        )
    return psycopg2.connect(url)
