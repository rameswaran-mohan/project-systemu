"""copy a scroll + activity + shadow scenario between two vault DBs.

Used by operators (and the shadow-execution shadow-mode regression test) to
clone a single end-to-end scenario from a source vault into an empty
destination vault — useful for reproducing bugs in isolation, for the
"shadow execution" verdict capture flow, and for sharing minimal repro
databases without leaking unrelated state.

What gets copied:
  - the named scroll
  - the activity rooted at that scroll (if assigned_shadow_id matches)
  - the named shadow
  - every tool referenced by ``shadow.available_tool_ids`` and
    ``activity.required_tool_ids``
  - every skill referenced by ``shadow.skill_ids`` and
    ``activity.required_skill_ids``

Usage:
  python scripts/copy_shadow_scenario.py SRC_URL DST_URL SCROLL_ID SHADOW_ID

The DBs are addressed via SQLAlchemy-style URLs (e.g. ``sqlite:///path``,
``postgresql://user:pw@host:5432/db``).  This script speaks the schema via
raw SQLAlchemy core so it stays insensitive to ORM-level imports — the only
shared knowledge is the column lists below.

Exit codes:
  0 — copied (or the rows already existed in dst — upsert is idempotent)
  1 — source row(s) missing or a SQL error occurred
  2 — bad CLI usage
"""
from __future__ import annotations

import json
import sys
from typing import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


# ─── Column lists (kept in sync with systemu/storage/sqlite/models.py) ──────

SCROLL_COLS = [
    "id", "name", "source_session_id", "raw_instructions_path",
    "narrative_md", "intent", "expected_outcome", "objectives", "constraints",
    "observed_preferences", "action_blocks", "activity_id", "status",
    "version", "tags", "pipeline_trace", "created_at", "updated_at",
]

ACTIVITY_COLS = [
    "id", "name", "scroll_id", "required_tool_ids", "required_skill_ids",
    "missing_tools", "assigned_shadow_id", "status", "intent_snapshot",
    "created_at", "updated_at",
]

SHADOW_COLS = [
    "id", "name", "description", "identity_block", "accumulated_voice",
    "system_prompt", "assigned_activity_ids", "available_tool_ids",
    "skill_ids", "status", "execution_log", "evolution_history",
    "memory_md_path", "memory_buffer_path", "supervisor_enabled", "specialty",
    "created_at", "updated_at",
]

TOOL_COLS = [
    "id", "name", "description", "tool_type", "parameters_schema",
    "return_schema", "implementation_notes", "dependencies",
    "implementation_path", "tool_md_path", "status", "forged_by_systemu",
    "enabled", "version", "dry_run_status", "dry_run_evidence",
    "last_successful_params", "evolution_history", "created_at", "updated_at",
]

SKILL_COLS = [
    "id", "name", "description", "category", "proficiency_level",
    "evidence_scroll_ids", "required_tool_ids", "required_tool_names",
    "instructions_md", "skill_md_path", "target_outcomes", "produces",
    "effectiveness_score", "skill_version", "evolution_history",
    "created_at", "updated_at",
]

# JSON-encoded columns — must be json.dumps'd on the way in when the dest
# backend is SQLite (raw text); SQLAlchemy's JSON type handles dict/list
# natively, but to stay backend-agnostic across the script we round-trip
# everything through JSON strings using parameter binding.
JSON_COLS = {
    "objectives", "constraints", "observed_preferences", "action_blocks",
    "tags", "pipeline_trace",
    "required_tool_ids", "required_skill_ids", "missing_tools",
    "assigned_activity_ids", "available_tool_ids", "skill_ids",
    "execution_log", "evolution_history",
    "parameters_schema", "return_schema", "dependencies",
    "dry_run_evidence", "last_successful_params",
    "evidence_scroll_ids", "required_tool_names",
    "target_outcomes", "produces",
}


# ─── Low-level helpers ──────────────────────────────────────────────────────

def fetch_one(eng: Engine, table: str, key: str, value: str, cols: list[str]):
    """Return a dict {col: value} for the row matching ``key=value`` or None."""
    sql = text(f"SELECT {', '.join(cols)} FROM {table} WHERE {key} = :v")
    with eng.connect() as conn:
        row = conn.execute(sql, {"v": value}).first()
    if not row:
        return None
    out = {}
    for col, val in zip(cols, row):
        if col in JSON_COLS and isinstance(val, str):
            try:
                out[col] = json.loads(val)
            except json.JSONDecodeError:
                out[col] = val
        else:
            out[col] = val
    return out


def _row_exists(eng: Engine, table: str, pk_col: str, pk_val) -> bool:
    sql = text(f"SELECT 1 FROM {table} WHERE {pk_col} = :v LIMIT 1")
    with eng.connect() as conn:
        return conn.execute(sql, {"v": pk_val}).first() is not None


def upsert(
    eng: Engine,
    table: str,
    row: dict,
    cols: list[str],
    json_cols: Iterable[str],
    *,
    pk_col: str = "id",
) -> bool:
    """Insert ``row`` into ``table`` if it doesn't already exist.

    Returns True when a new row was written, False when the row was already
    present (no-op — the script is idempotent).
    """
    if _row_exists(eng, table, pk_col, row.get(pk_col)):
        return False
    json_cols = set(json_cols)
    # SQLite stores booleans as 0/1; Postgres needs real bool.
    # Coerce known bool columns so SQLite -> Postgres copy works.
    BOOL_COLS = {
        "supervisor_enabled", "enabled", "forged_by_systemu",
        "baked_in_image",
    }
    params = {}
    for col in cols:
        val = row.get(col)
        if col in BOOL_COLS and val is not None:
            params[col] = bool(val)
        elif col in json_cols and not isinstance(val, (str, type(None))):
            params[col] = json.dumps(val)
        else:
            params[col] = val
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})")
    with eng.begin() as conn:
        conn.execute(sql, params)
    return True


# ─── Main ──────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(
            "usage: copy_shadow_scenario.py SRC_URL DST_URL SCROLL_ID SHADOW_ID",
            file=sys.stderr,
        )
        return 2

    _, src_url, dst_url, scroll_id, shadow_id = argv
    src = create_engine(src_url)
    dst = create_engine(dst_url)

    # Resolve source rows
    scroll = fetch_one(src, "scrolls", "id", scroll_id, SCROLL_COLS)
    if scroll is None:
        print(f"error: scroll {scroll_id!r} not found in source", file=sys.stderr)
        return 1
    shadow = fetch_one(src, "shadows", "id", shadow_id, SHADOW_COLS)
    if shadow is None:
        print(f"error: shadow {shadow_id!r} not found in source", file=sys.stderr)
        return 1

    activity = None
    # Find the activity that wires the scroll to the shadow.  We prefer the
    # one whose assigned_shadow_id matches the named shadow; failing that,
    # the first activity for the scroll.
    with src.connect() as conn:
        rows = conn.execute(
            text("SELECT id FROM activities WHERE scroll_id = :s"),
            {"s": scroll_id},
        ).fetchall()
    for (aid,) in rows:
        a = fetch_one(src, "activities", "id", aid, ACTIVITY_COLS)
        if a and a.get("assigned_shadow_id") == shadow_id:
            activity = a
            break
    if activity is None and rows:
        activity = fetch_one(src, "activities", "id", rows[0][0], ACTIVITY_COLS)

    # Resolve tools referenced by the shadow + activity
    tool_ids: set[str] = set()
    if shadow:
        tids = shadow.get("available_tool_ids") or []
        tool_ids.update(tids)
    if activity:
        tids = activity.get("required_tool_ids") or []
        tool_ids.update(tids)
    tools = []
    for tid in tool_ids:
        t = fetch_one(src, "tools", "id", tid, TOOL_COLS)
        if t:
            tools.append(t)
    print(f"resolved {len(tools)} tools")

    # resolve skills referenced by the shadow + activity.  Previous
    # versions of this script silently dropped these — the destination shadow
    # then warned "Skill skill_X not found in vault" at execution time.
    skill_ids: set[str] = set()
    if shadow:
        sids = shadow.get("skill_ids") or []
        skill_ids.update(sids)
    if activity:
        sids = activity.get("required_skill_ids") or []
        skill_ids.update(sids)
    skills = []
    for sid in skill_ids:
        sk = fetch_one(src, "skills", "id", sid, SKILL_COLS)
        if sk:
            skills.append(sk)
    print(f"resolved {len(skills)} skills")

    # Write to destination — tools + skills first so the FK-like activity /
    # shadow id arrays reference rows that already exist.
    n_tools = 0
    for t in tools:
        # env-specific fields don't carry across vault boundaries.
        # The daemon's first-boot sweep re-evaluates dry-run in the destination env.
        ENV_SPECIFIC_TOOL_FIELDS = ("dry_run_status", "dry_run_evidence", "last_successful_params")
        for f in ENV_SPECIFIC_TOOL_FIELDS:
            if f in t:
                t[f] = None
        if upsert(dst, "tools", t, TOOL_COLS, JSON_COLS):
            n_tools += 1
    print(f"copied {n_tools} tools")

    n_skills = 0
    for sk in skills:
        if upsert(dst, "skills", sk, SKILL_COLS, JSON_COLS):
            n_skills += 1
    print(f"copied {n_skills} skills")

    if upsert(dst, "scrolls", scroll, SCROLL_COLS, JSON_COLS):
        print(f"copied scroll {scroll['id']}")
    if upsert(dst, "shadows", shadow, SHADOW_COLS, JSON_COLS):
        print(f"copied shadow {shadow['id']}")
    if activity and upsert(dst, "activities", activity, ACTIVITY_COLS, JSON_COLS):
        print(f"copied activity {activity['id']}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
