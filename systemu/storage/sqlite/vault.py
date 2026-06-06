"""SqliteVault — IVault implementation backed by SQLAlchemy.

Supports both SQLite (local / hobbyist docker-compose) and PostgreSQL
(production) via the same connection URL:
  sqlite:///path/to/systemu.db
  postgresql+psycopg2://user:pass@host/dbname

Design decisions:
  - All entity data lives in the ORM tables defined in models.py.
  - Shadow and Elder memory blobs are stored in dedicated tables
    (shadow_memories, elder_memory) AND mirrored to the filesystem so
    shadow_runtime.py / metrics_tracker.py can still read them directly.
  - chat_history uses a proper table (one row per entry) keyed on 'ts'.
  - Sessions are opened per-operation with a context manager — thread-safe,
    no long-lived connections held.
  - Tables are auto-created on first connect (Base.metadata.create_all).
    Alembic handles subsequent schema evolution.

Usage:
    vault = SqliteVault("sqlite:///path/to/systemu.db", memory_dir=Path(...))
    # or let it infer memory_dir from the DB path:
    vault = SqliteVault("sqlite:///path/to/systemu.db")
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime

from systemu.core.utils import utcnow
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, event, func, select, delete, text
from sqlalchemy.orm import sessionmaker, Session

from systemu.core.models import (
    Activity, ActivityStatus,
    Evolution, EvolutionStatus,
    Notification, NotificationStatus,
    Scroll, ScrollStatus,
    Shadow, ShadowStatus,
    Skill,
    Tool, ToolStatus,
)
from systemu.storage.sqlite.models import (
    Base,
    ActivityRow,
    ChatHistoryRow,
    DecisionRow,
    ElderMemoryRow,
    EvolutionRow,
    NotificationRow,
    ScrollRow,
    ShadowMemoryRow,
    ShadowRow,
    SkillRow,
    ToolRow,
)

logger = logging.getLogger(__name__)

_ELDER_MEMORY_ID = 1   # ElderMemoryRow always uses row id=1


def _resolve_memory_dir(database_url: str, memory_dir: Optional[Path] = None) -> Path:
    """v0.6.6-d: resolve where ELDER_MEMORY.md + shadow_<id>/ memory dirs live.

    Resolution order:
      1. Explicit ``memory_dir`` parameter (operator override) — wins.
      2. ``sqlite:///...`` URL → ``<db_dir>/memory`` (next to the db file).
      3. ``postgresql://...`` URL → ``$SYSTEMU_VAULT_DIR/memory``.
         The vault dir is volume-mounted in docker modes (``vault_data:/data/vault``)
         so memory survives container restarts.  Before v0.6.6 this fell into
         the ``else`` branch and defaulted to ``/tmp/systemu_memory`` — the
         container's writable layer, lost on every ``docker compose down -v``
         or image rebuild.  See ``captures/E2E_VERDICT_DOCKER.md`` finding D.
      4. Anything else (truly unrecognized scheme) → ``/tmp/systemu_memory``
         with a warning.  Was the v0.6.5 default for all non-SQLite URLs;
         narrowed in v0.6.6 to genuine anomalies worth logging.
    """
    if memory_dir is not None:
        return Path(memory_dir)
    if database_url.startswith("sqlite:///"):
        return Path(database_url[len("sqlite:///"):]).parent / "memory"
    if database_url.startswith(("postgresql://", "postgres://")):
        vault_dir = os.environ.get("SYSTEMU_VAULT_DIR", "/data/vault")
        return Path(vault_dir) / "memory"
    logger.warning(
        "[SqliteVault] memory_dir not set for unrecognized URL scheme — using /tmp/systemu_memory"
    )
    return Path("/tmp/systemu_memory")


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers: Pydantic ↔ ORM row conversion
# ─────────────────────────────────────────────────────────────────────────────

def _dt(val: Any) -> datetime:
    """Coerce str/datetime to datetime."""
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        return datetime.fromisoformat(val)
    return utcnow()


def _scroll_to_row(s: Scroll) -> ScrollRow:
    data = s.model_dump(mode="json")
    return ScrollRow(
        id=s.id, name=s.name,
        source_session_id=s.source_session_id,
        raw_instructions_path=s.raw_instructions_path,
        narrative_md=s.narrative_md,
        intent=s.intent,
        expected_outcome=getattr(s, "expected_outcome", ""),
        objectives=data["objectives"],
        constraints=data["constraints"],
        observed_preferences=data["observed_preferences"],
        action_blocks=data["action_blocks"],
        activity_id=s.activity_id,
        status=s.status.value if hasattr(s.status, "value") else str(s.status),
        version=s.version,
        tags=data["tags"],
        # v0.6.5-a: round-trip the pipeline_trace
        pipeline_trace=data.get("pipeline_trace", []),
        created_at=_dt(s.created_at),
        updated_at=_dt(s.updated_at),
    )


def _row_to_scroll(r: ScrollRow) -> Scroll:
    return Scroll.model_validate({
        "id": r.id, "name": r.name,
        "source_session_id": r.source_session_id or "",
        "raw_instructions_path": r.raw_instructions_path or "",
        "narrative_md": r.narrative_md or "",
        "intent": r.intent or "",
        "expected_outcome": getattr(r, "expected_outcome", "") or "",
        "objectives": r.objectives or [],
        "constraints": r.constraints or {},
        "observed_preferences": r.observed_preferences or {},
        "action_blocks": r.action_blocks or [],
        "activity_id": r.activity_id,
        "status": r.status,
        "version": r.version,
        "tags": r.tags or [],
        # v0.6.5-a: load pipeline_trace; legacy rows have NULL → empty list
        "pipeline_trace": getattr(r, "pipeline_trace", None) or [],
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    })


def _scroll_header(r: ScrollRow) -> Dict[str, Any]:
    return {
        "id": r.id, "name": r.name, "status": r.status,
        "source_session_id": r.source_session_id or "",
        "created_at": r.created_at.isoformat() if r.created_at else "",
        "tags": r.tags or [],
        # v0.6.5-a: fast badge rendering — derive from pipeline_trace
        "has_warnings": any(
            (e.get("level") if isinstance(e, dict) else None) in ("warn", "error")
            for e in (getattr(r, "pipeline_trace", None) or [])
        ),
    }


def _tool_to_row(t: Tool) -> ToolRow:
    data = t.model_dump(mode="json")
    return ToolRow(
        id=t.id, name=t.name, description=t.description,
        tool_type=data["tool_type"],
        parameters_schema=data["parameters_schema"],
        return_schema=data["return_schema"],
        implementation_notes=t.implementation_notes,
        dependencies=data["dependencies"],
        implementation_path=t.implementation_path,
        tool_md_path=t.tool_md_path,
        status=data["status"],
        forged_by_systemu=t.forged_by_systemu,
        enabled=t.enabled,
        version=t.version,
        dry_run_status=getattr(t, "dry_run_status", "not_run"),
        dry_run_evidence=data.get("dry_run_evidence", {}),
        last_successful_params=data.get("last_successful_params", []),
        evolution_history=data.get("evolution_history", []),
        created_at=_dt(t.created_at),
        updated_at=_dt(t.updated_at),
    )


def _row_to_tool(r: ToolRow) -> Tool:
    return Tool.model_validate({
        "id": r.id, "name": r.name, "description": r.description or "",
        "tool_type": r.tool_type, "parameters_schema": r.parameters_schema or {},
        "return_schema": r.return_schema or {},
        "implementation_notes": r.implementation_notes or "",
        "dependencies": r.dependencies or [],
        "implementation_path": r.implementation_path or "",
        "tool_md_path": r.tool_md_path or "",
        "status": r.status,
        "forged_by_systemu": r.forged_by_systemu,
        "enabled": r.enabled,
        "version": r.version,
        "dry_run_status": getattr(r, "dry_run_status", None) or "not_run",
        "dry_run_evidence": getattr(r, "dry_run_evidence", None) or {},
        "last_successful_params": getattr(r, "last_successful_params", None) or [],
        "evolution_history": getattr(r, "evolution_history", None) or [],
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    })


def _tool_header(r: ToolRow) -> Dict[str, Any]:
    from systemu.vault.vault import _summarise_schema   # reuse canonical impl
    return {
        "id": r.id, "name": r.name, "description": r.description or "",
        "tool_type": r.tool_type,
        "parameter_names": list((r.parameters_schema or {}).keys()),
        "dependencies": r.dependencies or [],
        "status": r.status,
        "enabled": r.enabled,
        "forged_by_systemu": r.forged_by_systemu,
        # v0.5.0-a: dry-run status surfaces in the Tools page list
        "dry_run_status": getattr(r, "dry_run_status", None) or "not_run",
        "version": r.version,
        # v0.6.1-d: schema summaries inline in the header — see vault.py
        "parameters_schema_summary": _summarise_schema(r.parameters_schema or {}),
        "return_schema_summary":    _summarise_schema(r.return_schema or {}),
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }


def _skill_to_row(s: Skill) -> SkillRow:
    data = s.model_dump(mode="json")
    return SkillRow(
        id=s.id, name=s.name, description=s.description,
        category=s.category,
        proficiency_level=s.proficiency_level,
        evidence_scroll_ids=data["evidence_scroll_ids"],
        required_tool_ids=data["required_tool_ids"],
        required_tool_names=data["required_tool_names"],
        instructions_md=s.instructions_md,
        skill_md_path=s.skill_md_path,
        # v0.6.0-d.5: intent contract + telemetry fields.  getattr with
        # defaults keeps backward compat with older model instances.
        target_outcomes=getattr(s, "target_outcomes", []) or [],
        produces=getattr(s, "produces", []) or [],
        effectiveness_score=getattr(s, "effectiveness_score", 1.0),
        skill_version=getattr(s, "skill_version", 1),
        evolution_history=getattr(s, "evolution_history", []) or [],
        created_at=_dt(s.created_at),
        updated_at=_dt(s.updated_at),
    )


def _row_to_skill(r: SkillRow) -> Skill:
    return Skill.model_validate({
        "id": r.id, "name": r.name, "description": r.description or "",
        "category": r.category or "",
        "proficiency_level": r.proficiency_level or "intermediate",
        "evidence_scroll_ids": r.evidence_scroll_ids or [],
        "required_tool_ids": r.required_tool_ids or [],
        "required_tool_names": r.required_tool_names or [],
        "instructions_md": r.instructions_md or "",
        "skill_md_path": r.skill_md_path or "",
        # v0.6.0-d.5 — nullable columns; default sensibly for legacy rows
        "target_outcomes": getattr(r, "target_outcomes", None) or [],
        "produces":        getattr(r, "produces", None) or [],
        "effectiveness_score": getattr(r, "effectiveness_score", None) or 1.0,
        "skill_version":   getattr(r, "skill_version", None) or 1,
        "evolution_history": getattr(r, "evolution_history", None) or [],
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    })


def _skill_header(r: SkillRow) -> Dict[str, Any]:
    return {
        "id": r.id, "name": r.name, "description": r.description or "",
        "category": r.category or "",
        "proficiency_level": r.proficiency_level or "intermediate",
        "required_tool_names": r.required_tool_names or [],
        "required_tool_ids": r.required_tool_ids or [],
        "evidence_scroll_ids": r.evidence_scroll_ids or [],
        # v0.6.0-d.5: surface intent-contract fields in the index so the
        # validator catalog (Stage 6) can match without round-tripping
        # through the full skill record.
        "target_outcomes": getattr(r, "target_outcomes", None) or [],
        "produces":        getattr(r, "produces", None) or [],
        "effectiveness_score": getattr(r, "effectiveness_score", None) or 1.0,
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }


def _activity_to_row(a: Activity) -> ActivityRow:
    data = a.model_dump(mode="json")
    return ActivityRow(
        id=a.id, name=a.name, scroll_id=a.scroll_id,
        required_tool_ids=data["required_tool_ids"],
        required_skill_ids=data["required_skill_ids"],
        missing_tools=data["missing_tools"],
        assigned_shadow_id=a.assigned_shadow_id,
        status=data["status"],
        intent_snapshot=getattr(a, "intent_snapshot", ""),    # v0.6.0-f
        created_at=_dt(a.created_at),
        updated_at=_dt(a.updated_at),
    )


def _row_to_activity(r: ActivityRow) -> Activity:
    return Activity.model_validate({
        "id": r.id, "name": r.name, "scroll_id": r.scroll_id or "",
        "required_tool_ids": r.required_tool_ids or [],
        "required_skill_ids": r.required_skill_ids or [],
        "missing_tools": r.missing_tools or [],
        "assigned_shadow_id": r.assigned_shadow_id,
        "status": r.status,
        "intent_snapshot": getattr(r, "intent_snapshot", "") or "",    # v0.6.0-f
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    })


def _activity_header(r: ActivityRow) -> Dict[str, Any]:
    return {
        "id": r.id, "name": r.name, "scroll_id": r.scroll_id or "",
        "required_tool_ids": r.required_tool_ids or [],
        "required_skill_ids": r.required_skill_ids or [],
        "missing_tools": r.missing_tools or [],
        "assigned_shadow_id": r.assigned_shadow_id,
        "status": r.status,
        "intent_snapshot": getattr(r, "intent_snapshot", "") or "",    # v0.6.0-f
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }


def _shadow_to_row(s: Shadow, memory_md_path: str = "", memory_buffer_path: str = "") -> ShadowRow:
    data = s.model_dump(mode="json")
    return ShadowRow(
        id=s.id, name=s.name, description=s.description,
        identity_block=s.identity_block,
        accumulated_voice=s.accumulated_voice,
        # Mirror the composed runtime prompt into the legacy column so
        # any external reader still looking at `system_prompt` sees the
        # full value.  Internal reads always use identity_block /
        # accumulated_voice via the Pydantic model.
        system_prompt=s.system_prompt,
        assigned_activity_ids=data["assigned_activity_ids"],
        available_tool_ids=data["available_tool_ids"],
        skill_ids=data["skill_ids"],
        status=data["status"],
        execution_log=data["execution_log"],
        evolution_history=data["evolution_history"],
        memory_md_path=memory_md_path or s.memory_md_path,
        memory_buffer_path=memory_buffer_path or s.memory_buffer_path,
        supervisor_enabled=bool(getattr(s, "supervisor_enabled", False)),
        specialty=str(getattr(s, "specialty", "") or ""),
        created_at=_dt(s.created_at),
        updated_at=_dt(s.updated_at),
    )


def _row_to_shadow(r: ShadowRow) -> Shadow:
    # Backwards compat: when the row has identity_block populated, use it
    # directly.  When only the legacy system_prompt column is set
    # (pre-v0.3 data), the Pydantic model's migration validator transfers
    # it into identity_block.
    return Shadow.model_validate({
        "id": r.id, "name": r.name, "description": r.description or "",
        "identity_block": (
            getattr(r, "identity_block", "") or r.system_prompt or ""
        ),
        "accumulated_voice": getattr(r, "accumulated_voice", "") or "",
        "assigned_activity_ids": r.assigned_activity_ids or [],
        "available_tool_ids": r.available_tool_ids or [],
        "skill_ids": r.skill_ids or [],
        "status": r.status,
        "execution_log": r.execution_log or [],
        "evolution_history": r.evolution_history or [],
        "memory_md_path": r.memory_md_path or "",
        "memory_buffer_path": r.memory_buffer_path or "",
        "supervisor_enabled": bool(getattr(r, "supervisor_enabled", False) or False),
        "specialty": str(getattr(r, "specialty", "") or ""),
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    })


def _shadow_header(r: ShadowRow) -> Dict[str, Any]:
    return {
        "id": r.id, "name": r.name, "description": r.description or "",
        "status": r.status,
        "skill_ids": r.skill_ids or [],
        "tool_ids": r.available_tool_ids or [],
        "assigned_activity_ids": r.assigned_activity_ids or [],
        "activity_count": len(r.assigned_activity_ids or []),
        "memory_md_path": r.memory_md_path or "",
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }


def _evolution_to_row(e: Evolution) -> EvolutionRow:
    data = e.model_dump(mode="json")
    return EvolutionRow(
        id=e.id,
        evolution_type=data["evolution_type"],
        target_entity_type=e.target_entity_type,
        target_entity_ids=data["target_entity_ids"],
        description=e.description,
        rationale=e.rationale,
        before_snapshot=data["before_snapshot"],
        after_snapshot=data["after_snapshot"],
        status=data["status"],
        proposed_at=_dt(e.proposed_at),
        resolved_at=_dt(e.resolved_at) if e.resolved_at else None,
        edit_classification=e.edit_classification,
        fields_changed=data.get("fields_changed", []),
        reverted=e.reverted,
    )


def _row_to_evolution(r: EvolutionRow) -> Evolution:
    return Evolution.model_validate({
        "id": r.id, "evolution_type": r.evolution_type,
        "target_entity_type": r.target_entity_type or "",
        "target_entity_ids": r.target_entity_ids or [],
        "description": r.description or "",
        "rationale": r.rationale or "",
        "before_snapshot": r.before_snapshot or {},
        "after_snapshot": r.after_snapshot or {},
        "status": r.status,
        "proposed_at": r.proposed_at,
        "resolved_at": r.resolved_at,
        "edit_classification": getattr(r, "edit_classification", None),
        "fields_changed": getattr(r, "fields_changed", None) or [],
        "reverted": bool(getattr(r, "reverted", False)),
    })


def _evolution_header(r: EvolutionRow) -> Dict[str, Any]:
    return {
        "id": r.id, "evolution_type": r.evolution_type,
        "target_entity_type": r.target_entity_type or "",
        "description": r.description or "",
        "status": r.status,
        "proposed_at": r.proposed_at.isoformat() if r.proposed_at else "",
    }


def _notification_to_row(n: Notification) -> NotificationRow:
    data = n.model_dump(mode="json")
    return NotificationRow(
        id=n.id, title=n.title, message=n.message,
        actions=data["actions"],
        context=data["context"],
        status=data["status"],
        created_at=_dt(n.created_at),
        resolved_at=_dt(n.resolved_at) if n.resolved_at else None,
        resolution=n.resolution,
    )


def _row_to_notification(r: NotificationRow) -> Notification:
    return Notification.model_validate({
        "id": r.id, "title": r.title, "message": r.message or "",
        "actions": r.actions or [],
        "context": r.context or {},
        "status": r.status,
        "created_at": r.created_at,
        "resolved_at": r.resolved_at,
        "resolution": r.resolution,
    })


def _decision_to_row(d) -> "DecisionRow":
    """OperatorDecision → DecisionRow (v0.8.0 Pattern 1)."""
    return DecisionRow(
        id=d.id,
        title=d.title,
        body=d.body,
        options=list(d.options),
        context=dict(d.context),
        dedup_key=d.dedup_key,
        status=d.status,
        choice=d.choice,
        created_at=d.created_at,
        resolved_at=d.resolved_at,
    )


def _row_to_decision(r: "DecisionRow"):
    """DecisionRow → OperatorDecision (v0.8.0 Pattern 1)."""
    from systemu.approval.decision_queue import OperatorDecision
    return OperatorDecision(
        id=r.id,
        title=r.title or "",
        body=r.body or "",
        options=list(r.options or []),
        context=dict(r.context or {}),
        dedup_key=r.dedup_key or "",
        status=r.status or "pending",
        choice=r.choice,
        created_at=r.created_at,
        resolved_at=r.resolved_at,
    )


def _notification_header(r: NotificationRow) -> Dict[str, Any]:
    return {
        "id": r.id, "title": r.title, "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Upsert helper
# ─────────────────────────────────────────────────────────────────────────────

def _upsert(session: Session, row: Any) -> None:
    """Merge (upsert) a row by primary key — same semantics as session.merge()."""
    session.merge(row)


# ─────────────────────────────────────────────────────────────────────────────
#  Memory filesystem helpers
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, text: str) -> None:
    """Atomically write text to path using temp-file-rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_SHADOW_MEMORY_SCAFFOLD = """\
---
shadow_id: {shadow_id}
last_consolidated: {ts}
entry_count: 0
buffer_pending: 0
---

# Memory: {name}

## Self-Assessment

_No self-assessment yet — this shadow has not produced any executions._

## Heuristics

_No heuristics yet._

## Failure Patterns

_No failure patterns observed yet._

## Tool Quirks

_No tool quirks recorded yet._

## Domain Glossary

_No domain terms learned yet._
"""

_ELDER_MEMORY_SCAFFOLD = """\
---
last_consolidated: {ts}
entry_count: 0
buffer_pending: 0
---

# Elder Memory — Global Personalisation

## User Preferences

_No user preferences observed yet._

## Workflow Patterns

_No workflow patterns observed yet._

## Tool Affinities

_No tool affinities recorded yet._

## Recurring Variables

_No recurring variables observed yet._

## Personalisation Notes

_No personalisation notes yet._
"""


# ─────────────────────────────────────────────────────────────────────────────
#  SqliteVault
# ─────────────────────────────────────────────────────────────────────────────

class SqliteVault:
    """IVault backed by SQLAlchemy — supports SQLite and PostgreSQL.

    Args:
        database_url: SQLAlchemy connection URL.
            e.g. "sqlite:///path/to/systemu.db"
                 "postgresql+psycopg2://user:pass@host/db"
        memory_dir:   Directory for shadow/elder memory files.
            Defaults to <db_file_dir>/memory/.
            For in-memory (":memory:") or PostgreSQL, specify explicitly.
    """

    def __init__(
        self,
        database_url: str,
        memory_dir: Optional[Path] = None,
        *,
        strict_tier_types: bool = True,
    ) -> None:
        self._url = database_url
        self._strict_tier_types = bool(strict_tier_types)

        # v0.6.6-d: memory_dir resolution extracted into _resolve_memory_dir
        # so it's testable + Postgres URLs default to a volume-mounted path
        # instead of the container's volatile /tmp.
        self._memory_dir = _resolve_memory_dir(database_url, memory_dir)
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        # data_dir is the parent of memory_dir — used by notifications.py for event_log.jsonl
        self.data_dir: Path = self._memory_dir.parent

        connect_args: dict = {}
        is_sqlite = database_url.startswith("sqlite")
        if is_sqlite:
            connect_args["check_same_thread"] = False

        self._engine = create_engine(
            database_url,
            connect_args=connect_args,
            # pool_pre_ping: verify connection liveness before use — prevents
            # stale-connection errors after long idle periods or process forks.
            pool_pre_ping=True,
            echo=False,
            future=True,
        )

        # Enable SQLite WAL mode on every new connection.
        # Without WAL, dashboard reads and worker writes compete for the same
        # exclusive file lock → OperationalError: database is locked.
        # WAL allows concurrent readers + one writer; safe & durable.
        # Also set synchronous=NORMAL (safe with WAL, ~3× faster than FULL).
        if is_sqlite:
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _connection_record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)
        Base.metadata.create_all(self._engine)
        self._upgrade_schema()
        self._ensure_elder_memory()
        self._seed_from_file_vault_if_empty()

        # v0.9.1 (Layer 4): expose backend identity for the action-audit dispatch
        # layer in systemu/vault/backend/. The parent Vault.append_action_audit
        # reads these attrs via getattr(self, "_storage_backend", "file") to route
        # the call. SqliteVault handles both sqlite:// and postgresql:// URLs via
        # SQLAlchemy — parse the scheme to set the correct backend identity.
        if database_url.startswith("postgresql") or database_url.startswith("postgres"):
            self._storage_backend = "postgres"
            self._postgres_url = database_url
        else:
            self._storage_backend = "sqlite"
            self._sqlite_url = database_url  # mirrors self._url for the dispatch layer

        logger.info("[SqliteVault] Ready — %s", database_url)

    # ── First-boot seed ───────────────────────────────────────────────────────

    def _seed_from_file_vault_if_empty(self) -> None:
        """Populate an empty DB from the JSON starter vault on first boot.

        Why this exists:
            install.py writes SYSTEMU_STORAGE=sqlite by default, but the
            starter shadow_army / tools / skills / scrolls ship as JSON
            files under systemu/vault/ — loaded automatically by the FILE
            vault, NOT by SqliteVault.  Without this seed, the dashboard
            shows an empty vault on first boot and the operator has no
            visible content to work with.

            We treat this as a one-shot migration: if the DB has zero rows
            in the core tables AND a JSON vault exists alongside, copy
            everything across.  After the first successful import the
            seed silently no-ops on every subsequent boot because the
            tables aren't empty anymore.

        The actual copy goes through the same factored migrate() function
        the operator-facing JSON→DB migration tool calls — single source
        of truth for the import logic.
        """
        try:
            with self._session() as ses:
                non_empty = (
                    ses.query(ShadowRow).first()
                    or ses.query(ToolRow).first()
                    or ses.query(ScrollRow).first()
                    or ses.query(SkillRow).first()
                )
            if non_empty:
                return  # already seeded

            # Locate the JSON starter vault — look at SYSTEMU_VAULT_DIR
            # first (operator-overridable), then a couple of conventional
            # paths relative to the package.
            candidate_dirs = []
            env_dir = os.environ.get("SYSTEMU_VAULT_DIR")
            if env_dir:
                candidate_dirs.append(Path(env_dir))
            try:
                import systemu
                pkg_root = Path(systemu.__file__).resolve().parent
                candidate_dirs.append(pkg_root / "vault")
            except Exception:
                pass

            source = next((d for d in candidate_dirs if (d / "shadow_army" / "index.json").exists()), None)
            if source is None:
                logger.debug(
                    "[SqliteVault] empty DB but no JSON starter vault found "
                    "in any of %s — skipping seed",
                    candidate_dirs,
                )
                return

            logger.info("[SqliteVault] Empty DB detected — seeding from %s", source)
            from systemu.vault.vault import Vault as _FileVault
            file_vault = _FileVault(str(source))

            # IMPORTANT: file_vault.list_*() returns index *dicts* (id +
            # name + status + …), NOT the Pydantic models that save_*()
            # expects.  Hydrate each header via get_*() before passing it
            # across.  Orphan headers (index lists an entity whose JSON
            # file is absent) are logged + skipped — same contract as
            # the systemu.migrations.json_to_db tool.
            counts = {"scrolls": 0, "shadows": 0, "tools": 0, "skills": 0}
            orphans = {"scrolls": 0, "shadows": 0, "tools": 0, "skills": 0}

            def _seed_one(label: str, list_fn, get_fn, save_fn) -> None:
                for header in list_fn() or []:
                    entity_id = header.get("id") if isinstance(header, dict) else getattr(header, "id", None)
                    if not entity_id:
                        logger.warning("[SqliteVault] seed %s: index entry missing 'id'", label)
                        continue
                    try:
                        entity = get_fn(entity_id) if isinstance(header, dict) else header
                        save_fn(entity)
                        counts[label] += 1
                    except KeyError:
                        # Orphan header — index references a file that isn't there.
                        orphans[label] += 1
                        logger.debug(
                            "[SqliteVault] seed %s: orphan header %s (no on-disk file)",
                            label, entity_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[SqliteVault] seed %s failed on %s: %s",
                            label, entity_id, exc,
                        )

            _seed_one("scrolls", file_vault.list_scrolls,  file_vault.get_scroll,  self.save_scroll)
            _seed_one("shadows", file_vault.list_shadows,  file_vault.get_shadow,  self.save_shadow)
            _seed_one("tools",   file_vault.list_tools,    file_vault.get_tool,    self.save_tool)
            _seed_one("skills",  file_vault.list_skills,   file_vault.get_skill,   self.save_skill)

            orphan_total = sum(orphans.values())
            orphan_note = (
                f" ({orphan_total} orphan header{'' if orphan_total == 1 else 's'} skipped)"
                if orphan_total else ""
            )
            logger.info(
                "[SqliteVault] Seeded %d scrolls, %d shadows, %d tools, %d skills%s",
                counts["scrolls"], counts["shadows"], counts["tools"], counts["skills"],
                orphan_note,
            )
        except Exception as exc:
            # Never fail vault init because of seeding — log and continue.
            logger.warning("[SqliteVault] seed-on-empty failed (non-fatal): %s", exc)

    # ── Session context ───────────────────────────────────────────────────────

    def _session(self) -> Session:
        return self._Session()

    # ── Schema upgrades (additive migrations without Alembic) ─────────────────

    def _upgrade_schema(self) -> None:
        """Idempotently add columns introduced after initial schema creation.

        create_all() skips existing tables, so new columns must be added via
        ALTER TABLE.  SQLite does not support IF NOT EXISTS on ADD COLUMN, so
        we catch the OperationalError that fires when the column already exists.
        """
        new_cols = [
            ("evolutions", "edit_classification", "TEXT"),
            ("evolutions", "fields_changed",      "JSON DEFAULT '[]'"),
            ("evolutions", "reverted",             "INTEGER DEFAULT 0"),
        ]
        with self._engine.connect() as conn:
            for table, col, col_type in new_cols:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                    conn.commit()
                    logger.debug("[SqliteVault] Added column %s.%s", table, col)
                except Exception:
                    pass  # column already exists

    # ── Elder memory bootstrap ────────────────────────────────────────────────

    def _ensure_elder_memory(self) -> None:
        """Ensure the single elder_memory row exists."""
        with self._session() as s:
            row = s.get(ElderMemoryRow, _ELDER_MEMORY_ID)
            if row is None:
                scaffold = _ELDER_MEMORY_SCAFFOLD.format(ts=utcnow().isoformat())
                s.add(ElderMemoryRow(id=_ELDER_MEMORY_ID, memory_md=scaffold, memory_buffer_jsonl=""))
                s.commit()
                # Mirror to filesystem
                elder_file = self._memory_dir / "ELDER_MEMORY.md"
                if not elder_file.exists():
                    _atomic_write(elder_file, scaffold)

    # ── Memory file paths ─────────────────────────────────────────────────────

    def _shadow_memory_dir(self, shadow_id: str) -> Path:
        return self._memory_dir / f"shadow_{shadow_id}"

    def _shadow_memory_md_path(self, shadow_id: str) -> Path:
        return self._shadow_memory_dir(shadow_id) / "SHADOW_MEMORY.md"

    def _shadow_buffer_path(self, shadow_id: str) -> Path:
        return self._shadow_memory_dir(shadow_id) / "memory_buffer.jsonl"

    # ── load_index ────────────────────────────────────────────────────────────

    def load_index(self, entity: str) -> List[Dict[str, Any]]:
        """Return the lightweight header list for a given entity type."""
        with self._session() as s:
            if entity == "scrolls":
                rows = s.execute(select(ScrollRow)).scalars().all()
                return [_scroll_header(r) for r in rows]
            elif entity == "tools":
                rows = s.execute(select(ToolRow)).scalars().all()
                return [_tool_header(r) for r in rows]
            elif entity == "skills":
                rows = s.execute(select(SkillRow)).scalars().all()
                return [_skill_header(r) for r in rows]
            elif entity == "activities":
                rows = s.execute(select(ActivityRow)).scalars().all()
                return [_activity_header(r) for r in rows]
            elif entity == "shadow_army":
                rows = s.execute(select(ShadowRow)).scalars().all()
                return [_shadow_header(r) for r in rows]
            elif entity == "evolutions":
                rows = s.execute(select(EvolutionRow)).scalars().all()
                return [_evolution_header(r) for r in rows]
            elif entity == "notifications":
                rows = s.execute(select(NotificationRow)).scalars().all()
                return [_notification_header(r) for r in rows]
            elif entity == "decisions":
                rows = s.execute(select(DecisionRow)).scalars().all()
                return [
                    {
                        "id":         r.id,
                        "title":      r.title,
                        "dedup_key":  r.dedup_key,
                        "status":     r.status,
                        "options":    list(r.options or []),
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ]
            else:
                raise ValueError(f"Unknown entity type: {entity!r}")

    # ── Scroll ────────────────────────────────────────────────────────────────

    def save_scroll(self, scroll: Scroll) -> None:
        with self._session() as s:
            _upsert(s, _scroll_to_row(scroll))
            s.commit()
        logger.debug("[SqliteVault] Saved scroll %s (%s)", scroll.id, scroll.status)

    def get_scroll(self, scroll_id: str) -> Scroll:
        with self._session() as s:
            row = s.get(ScrollRow, scroll_id)
            if row is None:
                raise KeyError(f"Scroll not found: {scroll_id}")
            return _row_to_scroll(row)

    def list_scrolls(self, status: Optional[ScrollStatus] = None) -> List[Dict[str, Any]]:
        with self._session() as s:
            stmt = select(ScrollRow)
            if status:
                stmt = stmt.where(ScrollRow.status == status.value)
            rows = s.execute(stmt).scalars().all()
        return [_scroll_header(r) for r in rows]

    # ── Skill ─────────────────────────────────────────────────────────────────

    def save_skill(self, skill: Skill) -> None:
        # v0.6.1-e: batch resolution — single SELECT WHERE IN instead of N
        # find_tool_by_name() calls (each of which previously opened a
        # separate session).  Case-insensitive to match find_tool_by_name's
        # behaviour.  Unknown names are silently dropped (same as before).
        if skill.required_tool_names:
            lowered = [n.lower() for n in skill.required_tool_names]
            with self._session() as s:
                rows = s.execute(
                    select(ToolRow.id, ToolRow.name).where(
                        func.lower(ToolRow.name).in_(lowered)
                    )
                ).all()
            name_to_id = {row.name.lower(): row.id for row in rows}
            resolved_ids: List[str] = []
            for tname in skill.required_tool_names:
                tid = name_to_id.get(tname.lower())
                if tid:
                    resolved_ids.append(tid)
                else:
                    logger.debug(
                        "[SqliteVault] save_skill: tool %r not found yet", tname,
                    )
            skill.required_tool_ids = resolved_ids

        with self._session() as s:
            _upsert(s, _skill_to_row(skill))
            s.commit()
        logger.debug("[SqliteVault] Saved skill %s (%s)", skill.id, skill.name)

    def get_skill(self, skill_id: str) -> Skill:
        with self._session() as s:
            row = s.get(SkillRow, skill_id)
            if row is None:
                raise KeyError(f"Skill not found: {skill_id}")
            return _row_to_skill(row)

    def find_skill_by_name(self, name: str) -> Optional[Skill]:
        """Case-insensitive O(log N) lookup via SQL lower() — no full table scan."""
        with self._session() as s:
            row = s.execute(
                select(SkillRow).where(func.lower(SkillRow.name) == name.lower()).limit(1)
            ).scalars().first()
            if row is None:
                return None
            return _row_to_skill(row)

    def list_skills(self) -> List[Dict[str, Any]]:
        with self._session() as s:
            rows = s.execute(select(SkillRow)).scalars().all()
        return [_skill_header(r) for r in rows]

    # ── Tool ──────────────────────────────────────────────────────────────────

    def save_tool(self, tool: Tool) -> None:
        with self._session() as s:
            _upsert(s, _tool_to_row(tool))
            s.commit()
        logger.debug("[SqliteVault] Saved tool %s (%s)", tool.id, tool.name)

    def get_tool(self, tool_id: str) -> Tool:
        with self._session() as s:
            row = s.get(ToolRow, tool_id)
            if row is None:
                raise KeyError(f"Tool not found: {tool_id}")
            return _row_to_tool(row)

    def find_tool_by_name(self, name: str) -> Optional[Tool]:
        """Case-insensitive O(log N) lookup via SQL lower() — no full table scan."""
        with self._session() as s:
            row = s.execute(
                select(ToolRow).where(func.lower(ToolRow.name) == name.lower()).limit(1)
            ).scalars().first()
            if row is None:
                return None
            return _row_to_tool(row)

    # ── Decisions (v0.8.0 Pattern 1: OperatorDecisionQueue backing) ──────────

    def save_decision(self, decision) -> None:
        """Persist an OperatorDecision (upsert) into operator_decisions table."""
        from systemu.approval.decision_queue import OperatorDecision
        if not isinstance(decision, OperatorDecision):
            raise TypeError(f"expected OperatorDecision, got {type(decision).__name__}")
        with self._session() as s:
            _upsert(s, _decision_to_row(decision))
            s.commit()
        logger.debug("[SqliteVault] Saved decision %s (%s)", decision.id, decision.status)

    def get_decision(self, decision_id: str):
        """Load a single OperatorDecision by id."""
        with self._session() as s:
            row = s.get(DecisionRow, decision_id)
            if row is None:
                raise KeyError(f"decision {decision_id} not found")
            return _row_to_decision(row)

    # ── v0.6.8-a: recovery-engine duck-typed finders ─────────────────────────
    # The RecoveryEngine (systemu/recovery/engine.py) needs lightweight
    # find_* methods that return raw ORM rows (or None) without raising.
    # The engine only touches plain attributes (id, name, status, enabled,
    # dry_run_status, dry_run_evidence, execution_log, skill_ids,
    # available_tool_ids, required_tool_ids, required_skill_ids,
    # assigned_shadow_id), so we detach rows with expunge() to make them
    # safe to read after the session closes.

    def find_tool(self, tool_id: str):
        with self._session() as s:
            row = s.get(ToolRow, tool_id)
            if row is not None:
                s.expunge(row)
            return row

    def find_shadow(self, shadow_id: str):
        with self._session() as s:
            row = s.get(ShadowRow, shadow_id)
            if row is not None:
                s.expunge(row)
            return row

    def find_activity(self, activity_id: str):
        with self._session() as s:
            row = s.get(ActivityRow, activity_id)
            if row is not None:
                s.expunge(row)
            return row

    def find_activity_for_scroll(self, scroll_id: str):
        with self._session() as s:
            row = s.execute(
                select(ActivityRow).where(ActivityRow.scroll_id == scroll_id).limit(1)
            ).scalars().first()
            if row is not None:
                s.expunge(row)
            return row

    def find_scroll(self, scroll_id: str):
        with self._session() as s:
            row = s.get(ScrollRow, scroll_id)
            if row is not None:
                s.expunge(row)
            return row

    def skill_exists(self, skill_id: str) -> bool:
        with self._session() as s:
            return s.execute(
                select(SkillRow.id).where(SkillRow.id == skill_id).limit(1)
            ).first() is not None

    def list_tools(self, status: Optional[ToolStatus] = None) -> List[Dict[str, Any]]:
        with self._session() as s:
            stmt = select(ToolRow)
            if status:
                stmt = stmt.where(ToolRow.status == status.value)
            rows = s.execute(stmt).scalars().all()
        return [_tool_header(r) for r in rows]

    # ── Activity ──────────────────────────────────────────────────────────────

    def save_activity(self, activity: Activity) -> None:
        with self._session() as s:
            _upsert(s, _activity_to_row(activity))
            s.commit()
        logger.debug("[SqliteVault] Saved activity %s (%s)", activity.id, activity.name)

    def get_activity(self, activity_id: str) -> Activity:
        with self._session() as s:
            row = s.get(ActivityRow, activity_id)
            if row is None:
                raise KeyError(f"Activity not found: {activity_id}")
            return _row_to_activity(row)

    def list_activities(self, status: Optional[ActivityStatus] = None) -> List[Dict[str, Any]]:
        with self._session() as s:
            stmt = select(ActivityRow)
            if status:
                stmt = stmt.where(ActivityRow.status == status.value)
            rows = s.execute(stmt).scalars().all()
        return [_activity_header(r) for r in rows]

    # ── Shadow ────────────────────────────────────────────────────────────────

    def save_shadow(self, shadow: Shadow) -> None:
        sdir = self._shadow_memory_dir(shadow.id)
        sdir.mkdir(parents=True, exist_ok=True)

        md_path  = self._shadow_memory_md_path(shadow.id)
        buf_path = self._shadow_buffer_path(shadow.id)
        shadow.memory_md_path     = str(md_path)
        shadow.memory_buffer_path = str(buf_path)

        # Initialise scaffold if memory file doesn't exist yet
        if not md_path.exists():
            scaffold = _SHADOW_MEMORY_SCAFFOLD.format(
                shadow_id=shadow.id,
                ts=utcnow().isoformat(),
                name=shadow.name,
            )
            _atomic_write(md_path, scaffold)
            # Mirror scaffold to DB
            with self._session() as s:
                mem_row = s.get(ShadowMemoryRow, shadow.id)
                if mem_row is None:
                    s.add(ShadowMemoryRow(shadow_id=shadow.id, memory_md=scaffold, memory_buffer_jsonl=""))
                    s.commit()

        with self._session() as s:
            _upsert(s, _shadow_to_row(shadow, str(md_path), str(buf_path)))
            s.commit()
        logger.debug("[SqliteVault] Saved shadow %s (%s)", shadow.id, shadow.name)

    def get_shadow(self, shadow_id: str) -> Shadow:
        with self._session() as s:
            row = s.get(ShadowRow, shadow_id)
            if row is None:
                raise KeyError(f"Shadow not found: {shadow_id}")
            shadow = _row_to_shadow(row)
        # Ensure memory paths always point to our managed memory dir
        if not shadow.memory_md_path:
            shadow.memory_md_path = str(self._shadow_memory_md_path(shadow_id))
        if not shadow.memory_buffer_path:
            shadow.memory_buffer_path = str(self._shadow_buffer_path(shadow_id))
        return shadow

    def list_shadows(self, status: Optional[ShadowStatus] = None) -> List[Dict[str, Any]]:
        with self._session() as s:
            stmt = select(ShadowRow)
            if status:
                stmt = stmt.where(ShadowRow.status == status.value)
            rows = s.execute(stmt).scalars().all()
        return [_shadow_header(r) for r in rows]

    # ── Shadow memory ─────────────────────────────────────────────────────────

    def save_shadow_memory(self, shadow_id: str, memory_md: str) -> None:
        """Write consolidated SHADOW_MEMORY.md to DB + filesystem."""
        # Update DB row
        with self._session() as s:
            row = s.get(ShadowMemoryRow, shadow_id)
            if row is None:
                s.add(ShadowMemoryRow(shadow_id=shadow_id, memory_md=memory_md, memory_buffer_jsonl=""))
            else:
                row.memory_md = memory_md
            s.commit()
        # Mirror to filesystem so shadow_runtime / metrics_tracker can read it
        _atomic_write(self._shadow_memory_md_path(shadow_id), memory_md)

    def load_shadow_memory(self, shadow_id: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Return (MEMORY.md text, list of buffer entries)."""
        with self._session() as s:
            row = s.get(ShadowMemoryRow, shadow_id)

        md_text = ""
        buffer_jsonl = ""
        if row:
            md_text      = row.memory_md or ""
            buffer_jsonl = row.memory_buffer_jsonl or ""

        entries: List[Dict[str, Any]] = []
        for line in buffer_jsonl.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("[SqliteVault] Skipping malformed memory buffer line for shadow %s", shadow_id)
        return md_text, entries

    def append_memory_buffer(self, shadow_id: str, entry: Dict[str, Any]) -> None:
        """Append a lesson candidate to the shadow's memory buffer.

        Uses SELECT FOR UPDATE (or a row-level lock on SQLite via BEGIN IMMEDIATE)
        so two concurrent workers appending to the same shadow's buffer don't race
        on the read-modify-write of the JSONL text column.
        """
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._session() as s:
            # with_for_update() → SELECT ... FOR UPDATE on PostgreSQL,
            # acquires a deferred write lock on SQLite (serialises concurrent
            # appends on the same shadow row).
            row = s.execute(
                select(ShadowMemoryRow)
                .where(ShadowMemoryRow.shadow_id == shadow_id)
                .with_for_update()
            ).scalars().first()

            if row is None:
                s.add(ShadowMemoryRow(
                    shadow_id=shadow_id,
                    memory_md="",
                    memory_buffer_jsonl=line,
                ))
            else:
                row.memory_buffer_jsonl = (row.memory_buffer_jsonl or "") + line
            s.commit()

        # Mirror to filesystem so metrics_tracker.py can count buffer entries
        buf_path = self._shadow_buffer_path(shadow_id)
        buf_path.parent.mkdir(parents=True, exist_ok=True)
        with open(buf_path, "a", encoding="utf-8") as f:
            f.write(line)

    def clear_memory_buffer(self, shadow_id: str) -> None:
        """Clear shadow memory buffer after consolidation."""
        with self._session() as s:
            row = s.get(ShadowMemoryRow, shadow_id)
            if row is not None:
                row.memory_buffer_jsonl = ""
                s.commit()
        buf_path = self._shadow_buffer_path(shadow_id)
        if buf_path.exists():
            buf_path.write_text("", encoding="utf-8")

    def expunge_memory_entry(
        self,
        shadow_id: str,
        predicate,
        *,
        audit_path: "Optional[Path]" = None,
        reason: str = "operator_request",
    ) -> int:
        """Remove buffer entries matching ``predicate``.  Returns count.

        Contract-matches ``Vault.expunge_memory_entry`` so callers can use
        either backend interchangeably.  v0.4.0-a — required by the
        Intelligent Supervisor's live-write loop so confidently-wrong
        lessons can be retracted without daemon restart.
        """
        from datetime import datetime, timezone
        from pathlib import Path as _Path

        with self._session() as s:
            row = s.get(ShadowMemoryRow, shadow_id)
            raw = (row.memory_buffer_jsonl if row is not None else "") or ""

        if not raw.strip():
            return 0

        kept: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        for raw_line in raw.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                kept.append({"__raw__": raw_line})
                continue
            try:
                hit = bool(predicate(entry))
            except Exception:
                logger.exception(
                    "[SqliteVault] expunge predicate raised; keeping entry for safety",
                )
                hit = False
            if hit:
                removed.append(entry)
            else:
                kept.append(entry)

        if not removed:
            return 0

        new_lines = []
        for k in kept:
            if isinstance(k, dict) and "__raw__" in k and len(k) == 1:
                new_lines.append(k["__raw__"])
            else:
                new_lines.append(json.dumps(k, ensure_ascii=False))
        new_payload = "\n".join(new_lines) + ("\n" if new_lines else "")

        with self._session() as s:
            row = s.get(ShadowMemoryRow, shadow_id)
            if row is None:
                # Race: row vanished mid-call. Nothing to update; treat as 0.
                return 0
            row.memory_buffer_jsonl = new_payload
            s.commit()

        # Mirror the file copy if one exists (file vault sibling).
        try:
            buf_path = self._shadow_buffer_path(shadow_id)
            if buf_path.exists():
                buf_path.write_text(new_payload, encoding="utf-8")
        except Exception:
            logger.debug("[SqliteVault] expunge file-mirror update failed", exc_info=True)

        # Audit trail
        try:
            audit_target = audit_path or (
                _Path("data") / "audit" / "expunged_lessons.jsonl"
            )
            audit_target.parent.mkdir(parents=True, exist_ok=True)
            with audit_target.open("a", encoding="utf-8") as f:
                ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
                for r in removed:
                    f.write(json.dumps({
                        "ts":         ts,
                        "shadow_id":  shadow_id,
                        "reason":     reason,
                        "entry":      r,
                    }, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("[SqliteVault] could not write expunge audit log")

        logger.info(
            "[SqliteVault] expunged %d memory entries for shadow %s (reason=%s)",
            len(removed), shadow_id, reason,
        )
        return len(removed)

    # ── Memory tier gate-keepers (v0.2.2) ─────────────────────────────────────
    # Identical contract to Vault.append_shadow_memory_buffer /
    # append_elder_buffer — see docs/memory-model.md.  The validation is
    # delegated to systemu.core.memory_types.augment_buffer_entry so the
    # rules can never drift between file and database backends.

    def append_shadow_memory_buffer(
        self,
        shadow_id: str,
        entry: Dict[str, Any],
        *,
        source: str,
    ) -> Dict[str, Any]:
        from systemu.core.memory_types import (
            augment_buffer_entry,
            SHADOW_CLAIM_TYPES,
            ELDER_RECOMMENDED_TYPES,
        )
        augmented = augment_buffer_entry(
            entry,
            tier="shadow",
            source=source,
            allowed=SHADOW_CLAIM_TYPES,
            forbidden=ELDER_RECOMMENDED_TYPES,
            strict=self._strict_tier_types,
        )
        self.append_memory_buffer(shadow_id, augmented)
        return augmented

    def append_elder_buffer(
        self,
        entry: Dict[str, Any],
        *,
        source: str,
    ) -> Dict[str, Any]:
        from systemu.core.memory_types import (
            augment_buffer_entry,
            SHADOW_CLAIM_TYPES,
        )
        augmented = augment_buffer_entry(
            entry,
            tier="elder",
            source=source,
            allowed=frozenset(),
            forbidden=SHADOW_CLAIM_TYPES,
            strict=False,
        )
        self.append_elder_memory_buffer(augmented)
        return augmented

    # ── Prune old executions ──────────────────────────────────────────────────

    def prune_old_executions(self, max_keep: int = 50) -> int:
        """No-op for SQLite vault — executions are rows, not directories.

        The DB auto-handles storage; implement retention policy here if needed.
        """
        return 0

    # ── Elder memory ──────────────────────────────────────────────────────────

    def save_elder_memory(self, md_text: str) -> None:
        with self._session() as s:
            row = s.get(ElderMemoryRow, _ELDER_MEMORY_ID)
            if row is None:
                s.add(ElderMemoryRow(id=_ELDER_MEMORY_ID, memory_md=md_text, memory_buffer_jsonl=""))
            else:
                row.memory_md = md_text
            s.commit()
        _atomic_write(self._memory_dir / "ELDER_MEMORY.md", md_text)

    def load_elder_memory(self) -> str:
        with self._session() as s:
            row = s.get(ElderMemoryRow, _ELDER_MEMORY_ID)
        return (row.memory_md or "") if row else ""

    def append_elder_memory_buffer(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._session() as s:
            # FOR UPDATE serialises concurrent appends on the single elder row.
            row = s.execute(
                select(ElderMemoryRow)
                .where(ElderMemoryRow.id == _ELDER_MEMORY_ID)
                .with_for_update()
            ).scalars().first()
            if row is None:
                s.add(ElderMemoryRow(id=_ELDER_MEMORY_ID, memory_md="", memory_buffer_jsonl=line))
            else:
                row.memory_buffer_jsonl = (row.memory_buffer_jsonl or "") + line
            s.commit()

    def load_elder_memory_buffer(self) -> List[Dict[str, Any]]:
        with self._session() as s:
            row = s.get(ElderMemoryRow, _ELDER_MEMORY_ID)
        if not row or not row.memory_buffer_jsonl:
            return []
        entries: List[Dict[str, Any]] = []
        for line in row.memory_buffer_jsonl.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("[SqliteVault] Skipping malformed elder memory buffer line")
        return entries

    def clear_elder_memory_buffer(self) -> None:
        with self._session() as s:
            row = s.get(ElderMemoryRow, _ELDER_MEMORY_ID)
            if row is not None:
                row.memory_buffer_jsonl = ""
                s.commit()

    # ── Global memory aliases ─────────────────────────────────────────────────

    def load_global_memory(self) -> str:
        return self.load_elder_memory()

    def save_global_memory(self, md_text: str) -> None:
        self.save_elder_memory(md_text)

    def append_global_memory_buffer(self, entry: Dict[str, Any]) -> None:
        self.append_elder_memory_buffer(entry)

    def clear_global_memory_buffer(self) -> None:
        self.clear_elder_memory_buffer()

    # ── Chat history ──────────────────────────────────────────────────────────

    def append_chat_history(self, entry: Dict[str, Any]) -> None:
        ts = str(entry.get("ts", utcnow().isoformat()))
        with self._session() as s:
            s.add(ChatHistoryRow(ts=ts, data=entry))
            s.commit()

    def load_chat_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._session() as s:
            rows = (
                s.execute(
                    select(ChatHistoryRow)
                    .order_by(ChatHistoryRow.rowid.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        # rows are newest-first; reverse to return oldest-first (same as file vault)
        return [r.data for r in reversed(rows)]

    def update_chat_history_entry(self, ts: str, updates: Dict[str, Any]) -> None:
        with self._session() as s:
            rows = s.execute(
                select(ChatHistoryRow).where(ChatHistoryRow.ts == ts)
            ).scalars().all()
            for row in rows:
                merged = dict(row.data)
                merged.update(updates)
                row.data = merged
            s.commit()

    def get_latest_chat_scroll(self) -> Optional[Scroll]:
        history = self.load_chat_history(limit=1)
        if not history:
            return None
        scroll_id = history[-1].get("scroll_id")
        if not scroll_id:
            return None
        try:
            return self.get_scroll(scroll_id)
        except KeyError:
            return None

    # ── Evolution ─────────────────────────────────────────────────────────────

    def save_evolution(self, evolution: Evolution) -> None:
        with self._session() as s:
            _upsert(s, _evolution_to_row(evolution))
            s.commit()

    def get_evolution(self, evolution_id: str) -> Evolution:
        with self._session() as s:
            row = s.get(EvolutionRow, evolution_id)
            if row is None:
                raise KeyError(f"Evolution not found: {evolution_id}")
            return _row_to_evolution(row)

    def list_evolutions(self, status: Optional[EvolutionStatus] = None) -> List[Dict[str, Any]]:
        with self._session() as s:
            stmt = select(EvolutionRow)
            if status:
                stmt = stmt.where(EvolutionRow.status == status.value)
            rows = s.execute(stmt).scalars().all()
        return [_evolution_header(r) for r in rows]

    # ── Notification ──────────────────────────────────────────────────────────

    def queue_notification(self, notification: Notification) -> None:
        with self._session() as s:
            _upsert(s, _notification_to_row(notification))
            s.commit()

    def resolve_notification(self, notification_id: str, resolution: str) -> None:
        with self._session() as s:
            row = s.get(NotificationRow, notification_id)
            if row is not None:
                row.status     = NotificationStatus.RESOLVED.value
                row.resolution = resolution
                row.resolved_at = utcnow()
            s.commit()

    def list_pending_notifications(self) -> List[Dict[str, Any]]:
        with self._session() as s:
            rows = s.execute(
                select(NotificationRow).where(NotificationRow.status == NotificationStatus.PENDING.value)
            ).scalars().all()
        return [_notification_header(r) for r in rows]

    # ── v0.9.1 action-audit dispatch ─────────────────────────────────────────

    def append_action_audit(self, entry: dict) -> None:
        """Route one audit entry to the sqlite or postgres dispatch layer.

        SqliteVault handles both backends (SQLAlchemy supports both); the
        correct dispatch module is selected from self._storage_backend which
        is set in __init__ based on the URL scheme.

        ``entry`` MUST contain: ts (ISO), execution_id, objective_id, action,
        params (dict), success (bool), error (Optional[str]).
        ``user_id`` is optional.
        """
        from systemu.vault.backend import dispatch_append_action_audit
        dispatch_append_action_audit(self, entry)

    def query_action_audit(
        self,
        *,
        execution_id: str,
        since_ts: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list:
        """Return audit entries matching the given filters, in append order.

        Delegates to the sqlite or postgres dispatch backend. Filters are
        AND-combined. Returns [] if no audit log exists yet.
        """
        from systemu.vault.backend import dispatch_query_action_audit
        return dispatch_query_action_audit(
            self, execution_id=execution_id,
            since_ts=since_ts, user_id=user_id,
        )

    # ── Dispose (cleanup) ─────────────────────────────────────────────────────

    def dispose(self) -> None:
        """Dispose the engine connection pool — call on app shutdown."""
        self._engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
#  v0.6.8-d — Seed tool_dep_approvals from requirements-tools.txt
# ─────────────────────────────────────────────────────────────────────────────

def seed_tool_dep_approvals(database_url: str, requirements_path) -> int:
    """Seed tool_dep_approvals from a requirements-tools.txt file. Idempotent.

    The installer wizard writes ``tools/requirements-tools.txt`` after the
    operator approves the scanned tool deps.  At daemon boot we mirror that
    file into the ``tool_dep_approvals`` table so the dashboard's runtime
    approval workflow (D2) sees the pre-approved set as already accepted.

    Returns number of rows inserted.
    """
    import re
    import uuid
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from .models import ToolDepApproval

    requirements_path = Path(requirements_path)
    if not requirements_path.exists():
        return 0

    engine = create_engine(database_url)
    inserted = 0
    with Session(engine) as s:
        existing = {r.package_name for r in s.query(ToolDepApproval).all()}
        for line in requirements_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([<>=!~].*)?$", line)
            if not m:
                continue
            name = m.group(1)
            spec = m.group(2)
            if name in existing:
                continue
            s.add(ToolDepApproval(
                id=f"dep_{uuid.uuid4().hex[:8]}",
                package_name=name,
                package_version_spec=spec,
                approved_by="wizard",
                source="wizard",
                baked_in_image=True,
            ))
            existing.add(name)
            inserted += 1
        s.commit()
    return inserted
