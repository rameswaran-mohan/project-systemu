"""sqlite_backend.py — lightweight sqlite3 dispatch functions for vault tables.

Used by systemu.vault.backend dispatch layer when vault._storage_backend == 'sqlite'.
Intentionally avoids SQLAlchemy — this is a thin direct sqlite3 module so it has
zero extra dependencies beyond the stdlib and works identically on every platform.

Public surface:
    ensure_schema(vault)           — idempotent DDL (called once at vault init time)
    dispatch_append_action_audit(vault, entry) — INSERT one audit row
    dispatch_query_action_audit(vault, *, execution_id, since_ts, user_id) — SELECT audit rows
    dispatch_append_session_summary(vault, summary) — INSERT one session_summary row
    dispatch_query_session_summaries(vault, *, user_id, status, since_ts, limit) — SELECT rows
    dispatch_search_session_summaries(vault, *, query, user_id, limit) — FTS5 search
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

_SESSION_SUMMARIES_DDL = """
CREATE TABLE IF NOT EXISTS session_summaries (
    id              TEXT    PRIMARY KEY,
    session_id      TEXT    NOT NULL,
    execution_id    TEXT,
    user_id         TEXT,
    started_at      TEXT    NOT NULL,
    completed_at    TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    intent          TEXT    NOT NULL,
    outcome_summary TEXT    NOT NULL,
    key_facts_json  TEXT    NOT NULL,
    files_json      TEXT    NOT NULL,
    tags_json       TEXT    NOT NULL,
    raw_chat_id     TEXT
);
CREATE INDEX IF NOT EXISTS ix_session_summaries_user_completed
    ON session_summaries (user_id, completed_at);
CREATE INDEX IF NOT EXISTS ix_session_summaries_status
    ON session_summaries (status);

CREATE VIRTUAL TABLE IF NOT EXISTS session_summaries_fts USING fts5(
    session_id UNINDEXED,
    user_id    UNINDEXED,
    intent,
    outcome_summary,
    tags,
    content='session_summaries',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS session_summaries_fts_ins
    AFTER INSERT ON session_summaries
BEGIN
    INSERT INTO session_summaries_fts (rowid, session_id, user_id, intent, outcome_summary, tags)
    VALUES (new.rowid, new.session_id, new.user_id, new.intent, new.outcome_summary, new.tags_json);
END;
"""


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def ensure_schema(vault) -> None:
    """Idempotent: ensure all sqlite tables exist on the vault's db.

    Runs both v0.9.1 (action_audit) and v0.9.2 (session_summaries + FTS5)
    DDL. Called once during vault fixture setup (test) or vault factory init
    (production). Safe to call multiple times — CREATE TABLE IF NOT EXISTS,
    CREATE INDEX IF NOT EXISTS, and CREATE VIRTUAL TABLE IF NOT EXISTS are all
    idempotent. CREATE TRIGGER IF NOT EXISTS likewise.
    """
    conn = _connect(vault)
    try:
        conn.executescript(_ACTION_AUDIT_DDL)
        conn.executescript(_SESSION_SUMMARIES_DDL)
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


def dispatch_append_session_summary(vault, summary) -> None:
    """Insert one session_summary row.

    Called from vault.append_session_summary when storage_backend == 'sqlite'.
    ``summary`` is a SessionSummary Pydantic model instance.
    """
    conn = _connect(vault)
    try:
        conn.execute(
            "INSERT INTO session_summaries "
            "(id, session_id, execution_id, user_id, started_at, completed_at, "
            "status, intent, outcome_summary, key_facts_json, files_json, tags_json, raw_chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                summary.id, summary.session_id, summary.execution_id, summary.user_id,
                summary.started_at.isoformat(), summary.completed_at.isoformat(),
                summary.status, summary.intent, summary.outcome_summary,
                json.dumps(summary.key_facts_learned or []),
                json.dumps(summary.files_produced or []),
                json.dumps(summary.tags or []),
                summary.raw_chat_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def dispatch_query_session_summaries(vault, *, user_id=None, status=None,
                                      since_ts=None, limit=None):
    """Return matching session_summary rows ordered by insertion order (rowid ASC).

    Filters are AND-combined:
      user_id   — optional, equality match
      status    — optional, equality match
      since_ts  — optional, inclusive lower bound on completed_at (datetime)
    """
    from systemu.core.models import SessionSummary
    from datetime import datetime as _dt

    sql = (
        "SELECT id, session_id, execution_id, user_id, started_at, completed_at, "
        "status, intent, outcome_summary, key_facts_json, files_json, tags_json, "
        "raw_chat_id FROM session_summaries WHERE 1=1"
    )
    args = []
    if user_id is not None:
        sql += " AND user_id = ?"
        args.append(user_id)
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    if since_ts is not None:
        sql += " AND completed_at >= ?"
        args.append(since_ts.isoformat())
    sql += " ORDER BY rowid ASC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    conn = _connect(vault)
    try:
        cur = conn.execute(sql, args)
        rows = cur.fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        out.append(SessionSummary(
            id=r[0], session_id=r[1], execution_id=r[2], user_id=r[3],
            started_at=_dt.fromisoformat(r[4]),
            completed_at=_dt.fromisoformat(r[5]),
            status=r[6], intent=r[7], outcome_summary=r[8],
            key_facts_learned=json.loads(r[9] or "[]"),
            files_produced=json.loads(r[10] or "[]"),
            tags=json.loads(r[11] or "[]"),
            raw_chat_id=r[12],
        ))
    return out


def dispatch_search_session_summaries(vault, *, query, user_id=None, limit=5):
    """FTS5 keyword search over intent, outcome_summary, and tags.

    Returns a list of SessionSummary instances ordered by FTS5 rank
    (most relevant first). Returns [] for empty/blank queries.
    """
    from systemu.core.models import SessionSummary
    from datetime import datetime as _dt

    if not query or not query.strip():
        return []

    base = (
        "SELECT s.id, s.session_id, s.execution_id, s.user_id, s.started_at, "
        "s.completed_at, s.status, s.intent, s.outcome_summary, "
        "s.key_facts_json, s.files_json, s.tags_json, s.raw_chat_id "
        "FROM session_summaries s "
        "JOIN session_summaries_fts f ON f.rowid = s.rowid "
        "WHERE session_summaries_fts MATCH ?"
    )
    args = [query.strip()]
    if user_id is not None:
        base += " AND s.user_id = ?"
        args.append(user_id)
    base += f" ORDER BY rank LIMIT {int(limit)}"

    conn = _connect(vault)
    try:
        cur = conn.execute(base, args)
        rows = cur.fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        out.append(SessionSummary(
            id=r[0], session_id=r[1], execution_id=r[2], user_id=r[3],
            started_at=_dt.fromisoformat(r[4]),
            completed_at=_dt.fromisoformat(r[5]),
            status=r[6], intent=r[7], outcome_summary=r[8],
            key_facts_learned=json.loads(r[9] or "[]"),
            files_produced=json.loads(r[10] or "[]"),
            tags=json.loads(r[11] or "[]"),
            raw_chat_id=r[12],
        ))
    return out


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
