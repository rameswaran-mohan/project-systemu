"""SQLAlchemy ORM models — mirrors all Pydantic entities in systemu/core/models.py.

Design rules:
  - String PKs (same UUIDs as file vault — no autoincrement) for zero-friction migration.
  - JSON columns for List/Dict fields (native JSON in PostgreSQL, TEXT JSON in SQLite).
  - Separate memory tables for large/frequently-updated blobs (shadow_memories,
    elder_memory) so row scans on entity tables stay cheap.
  - chat_history is a proper table keyed on 'ts' to support in-place updates.

The Base is shared between SqliteVault and Alembic env.py.
Call `Base.metadata.create_all(engine)` on startup; Alembic handles schema
evolution after the initial creation.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ─────────────────────────────────────────────────────────────────────────────
#  Shared declarative base
# ─────────────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Entity tables
# ─────────────────────────────────────────────────────────────────────────────

class ScrollRow(Base):
    __tablename__ = "scrolls"

    id:                    Mapped[str]      = mapped_column(String, primary_key=True)
    name:                  Mapped[str]      = mapped_column(String, nullable=False)
    source_session_id:     Mapped[str]      = mapped_column(String, default="")
    raw_instructions_path: Mapped[str]      = mapped_column(Text, default="")
    narrative_md:          Mapped[str]      = mapped_column(Text, default="")
    intent:                Mapped[str]      = mapped_column(Text, default="")
    # concrete "what success looks like" description.  Nullable for
    # legacy rows so the migration is safe.
    expected_outcome:      Mapped[str]      = mapped_column(Text, default="", nullable=True)
    objectives:            Mapped[list]     = mapped_column(JSON, default=list)
    constraints:           Mapped[dict]     = mapped_column(JSON, default=dict)
    observed_preferences:  Mapped[dict]     = mapped_column(JSON, default=dict)
    action_blocks:         Mapped[list]     = mapped_column(JSON, default=list)
    activity_id:           Mapped[str|None] = mapped_column(String, nullable=True)
    status:                Mapped[str]      = mapped_column(String, default="draft")
    version:               Mapped[int]      = mapped_column(Integer, default=1)
    tags:                  Mapped[list]     = mapped_column(JSON, default=list)
    # per-stage pipeline observability events.  Nullable for
    # legacy rows so the migration is safe.
    pipeline_trace:        Mapped[list]     = mapped_column(JSON, default=list, nullable=True)
    created_at:            Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at:            Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class ToolRow(Base):
    __tablename__ = "tools"

    id:                   Mapped[str]      = mapped_column(String, primary_key=True)
    name:                 Mapped[str]      = mapped_column(String, nullable=False, index=True)
    description:          Mapped[str]      = mapped_column(Text, default="")
    tool_type:            Mapped[str]      = mapped_column(String, default="python_function")
    parameters_schema:    Mapped[dict]     = mapped_column(JSON, default=dict)
    return_schema:        Mapped[dict]     = mapped_column(JSON, default=dict)
    implementation_notes: Mapped[str]      = mapped_column(Text, default="")
    dependencies:         Mapped[list]     = mapped_column(JSON, default=list)
    implementation_path:  Mapped[str]      = mapped_column(Text, default="")
    tool_md_path:         Mapped[str]      = mapped_column(Text, default="")
    status:               Mapped[str]      = mapped_column(String, default="proposed")
    forged_by_systemu:    Mapped[bool]     = mapped_column(Boolean, default=False)
    enabled:              Mapped[bool]     = mapped_column(Boolean, default=False)
    version:              Mapped[int]      = mapped_column(Integer, default=1)
    # / -b: dry-run gate + observed-success replay + evolution audit.
    # Nullable so existing rows (pre-migration) read as defaults.
    dry_run_status:        Mapped[str]      = mapped_column(String, default="not_run", nullable=True)
    dry_run_evidence:      Mapped[dict]     = mapped_column(JSON, default=dict, nullable=True)
    last_successful_params: Mapped[list]    = mapped_column(JSON, default=list, nullable=True)
    evolution_history:     Mapped[list]     = mapped_column(JSON, default=list, nullable=True)
    created_at:           Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at:           Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class SkillRow(Base):
    __tablename__ = "skills"

    id:                  Mapped[str]      = mapped_column(String, primary_key=True)
    name:                Mapped[str]      = mapped_column(String, nullable=False, index=True)
    description:         Mapped[str]      = mapped_column(Text, default="")
    category:            Mapped[str]      = mapped_column(String, default="")
    proficiency_level:   Mapped[str]      = mapped_column(String, default="intermediate")
    evidence_scroll_ids: Mapped[list]     = mapped_column(JSON, default=list)
    required_tool_ids:   Mapped[list]     = mapped_column(JSON, default=list)
    required_tool_names: Mapped[list]     = mapped_column(JSON, default=list)
    instructions_md:     Mapped[str]      = mapped_column(Text, default="")
    skill_md_path:       Mapped[str]      = mapped_column(Text, default="")
    # v0.6.0-d.5: intent-contract fields + runtime telemetry.  All nullable
    # for safe migration; the Skill model defaults are applied when reading
    # legacy rows back.  NOT exported to portable SKILL.md (lives only here).
    target_outcomes:     Mapped[list]     = mapped_column(JSON, default=list, nullable=True)
    produces:            Mapped[list]     = mapped_column(JSON, default=list, nullable=True)
    effectiveness_score: Mapped[float]    = mapped_column(default=1.0, nullable=True)
    skill_version:       Mapped[int]      = mapped_column(Integer, default=1, nullable=True)
    evolution_history:   Mapped[list]     = mapped_column(JSON, default=list, nullable=True)
    created_at:          Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at:          Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class ActivityRow(Base):
    __tablename__ = "activities"

    id:                 Mapped[str]      = mapped_column(String, primary_key=True)
    name:               Mapped[str]      = mapped_column(String, nullable=False)
    scroll_id:          Mapped[str]      = mapped_column(String, default="")
    required_tool_ids:  Mapped[list]     = mapped_column(JSON, default=list)
    required_skill_ids: Mapped[list]     = mapped_column(JSON, default=list)
    missing_tools:      Mapped[list]     = mapped_column(JSON, default=list)
    assigned_shadow_id: Mapped[str|None] = mapped_column(String, nullable=True)
    status:             Mapped[str]      = mapped_column(String, default="unassigned")
    # frozen intent at extraction time so Stage 5 (shadow tiebreak)
    # can match on intent without re-loading the scroll on every decision.
    intent_snapshot:    Mapped[str]      = mapped_column(Text, default="", nullable=True)
    created_at:         Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at:         Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class ShadowRow(Base):
    __tablename__ = "shadows"

    id:                    Mapped[str]      = mapped_column(String, primary_key=True)
    name:                  Mapped[str]      = mapped_column(String, nullable=False)
    description:           Mapped[str]      = mapped_column(Text, default="")
    # Identity tier (v0.3) — operator-editable + consolidator-grown.
    # The legacy ``system_prompt`` column is preserved for backwards
    # compatibility during the migration window; the runtime composes
    # the prompt from identity_block + accumulated_voice and falls back
    # to system_prompt when identity_block is empty (pre-migration data).
    identity_block:        Mapped[str]      = mapped_column(Text, default="")
    accumulated_voice:     Mapped[str]      = mapped_column(Text, default="")
    system_prompt:         Mapped[str]      = mapped_column(Text, default="")
    assigned_activity_ids: Mapped[list]     = mapped_column(JSON, default=list)
    available_tool_ids:    Mapped[list]     = mapped_column(JSON, default=list)
    skill_ids:             Mapped[list]     = mapped_column(JSON, default=list)
    status:                Mapped[str]      = mapped_column(String, default="dormant")
    execution_log:         Mapped[list]     = mapped_column(JSON, default=list)
    evolution_history:     Mapped[list]     = mapped_column(JSON, default=list)
    # memory_md_path / memory_buffer_path are kept as filesystem paths so
    # shadow_runtime.py and metrics_tracker.py can still read the files directly.
    # SqliteVault writes memory blobs to BOTH the DB (shadow_memories table) and
    # the filesystem path so both access patterns work.
    memory_md_path:        Mapped[str]      = mapped_column(Text, default="")
    memory_buffer_path:    Mapped[str]      = mapped_column(Text, default="")
    # per-shadow opt-in for the Intelligent Supervisor.  Nullable so
    # existing rows (pre-migration) read as False without an UPDATE.
    supervisor_enabled:    Mapped[bool]     = mapped_column(Boolean, default=False, nullable=True)
    # operator-labelled specialty for routing preference.
    specialty:             Mapped[str]      = mapped_column(Text, default="", nullable=True)
    created_at:            Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at:            Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class EvolutionRow(Base):
    __tablename__ = "evolutions"

    id:                 Mapped[str]           = mapped_column(String, primary_key=True)
    evolution_type:     Mapped[str]           = mapped_column(String, default="upgrade")
    target_entity_type: Mapped[str]           = mapped_column(String, default="")
    target_entity_ids:  Mapped[list]          = mapped_column(JSON, default=list)
    description:        Mapped[str]           = mapped_column(Text, default="")
    rationale:          Mapped[str]           = mapped_column(Text, default="")
    before_snapshot:    Mapped[dict]          = mapped_column(JSON, default=dict)
    after_snapshot:     Mapped[dict]          = mapped_column(JSON, default=dict)
    status:             Mapped[str]           = mapped_column(String, default="proposed")
    proposed_at:        Mapped[datetime]      = mapped_column(DateTime, default=func.now())
    resolved_at:        Mapped[datetime|None] = mapped_column(DateTime, nullable=True)
    # Phase 1 workshop edit provenance
    edit_classification: Mapped[str | None]  = mapped_column(String, nullable=True)
    fields_changed:      Mapped[list]        = mapped_column(JSON, default=list)
    reverted:            Mapped[bool]        = mapped_column(Boolean, default=False)


class NotificationRow(Base):
    __tablename__ = "notifications"

    id:          Mapped[str]           = mapped_column(String, primary_key=True)
    title:       Mapped[str]           = mapped_column(String, nullable=False)
    message:     Mapped[str]           = mapped_column(Text, default="")
    actions:     Mapped[list]          = mapped_column(JSON, default=list)
    context:     Mapped[dict]          = mapped_column(JSON, default=dict)
    status:      Mapped[str]           = mapped_column(String, default="pending", index=True)
    created_at:  Mapped[datetime]      = mapped_column(DateTime, default=func.now())
    resolved_at: Mapped[datetime|None] = mapped_column(DateTime, nullable=True)
    resolution:  Mapped[str|None]      = mapped_column(String, nullable=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Memory tables (large blobs, separated for efficient row scans)
# ─────────────────────────────────────────────────────────────────────────────

class ShadowMemoryRow(Base):
    """Stores consolidated SHADOW_MEMORY.md + pending buffer entries per shadow.

    memory_buffer_jsonl is a JSONL string (one JSON object per line) mirroring
    the memory_buffer.jsonl file pattern used by the file vault.
    """
    __tablename__ = "shadow_memories"

    shadow_id:           Mapped[str] = mapped_column(String, primary_key=True)
    memory_md:           Mapped[str] = mapped_column(Text, default="")
    memory_buffer_jsonl: Mapped[str] = mapped_column(Text, default="")


class ElderMemoryRow(Base):
    """Single-row table for global elder memory.

    Always row id=1.  Use upsert (INSERT OR REPLACE / ON CONFLICT DO UPDATE).
    """
    __tablename__ = "elder_memory"

    id:                  Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    memory_md:           Mapped[str] = mapped_column(Text, default="")
    memory_buffer_jsonl: Mapped[str] = mapped_column(Text, default="")


class ChatHistoryRow(Base):
    """One row per chat submission.  ts is the application-level timestamp key
    used for in-place updates (matches the 'ts' field inside the JSON entry).
    """
    __tablename__ = "chat_history"

    # Auto-increment rowid for ordering; ts for app-level lookup/update
    rowid:   Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts:      Mapped[str]      = mapped_column(String, nullable=False, index=True)
    data:    Mapped[dict]     = mapped_column(JSON, nullable=False)
    created: Mapped[datetime] = mapped_column(DateTime, default=func.now())


# ─────────────────────────────────────────────────────────────────────────────
#  Phase 3 — Cross-process event streaming + approval gate
# ─────────────────────────────────────────────────────────────────────────────

class EventRow(Base):
    """One row per published event.  Consumed by the dashboard event-bridge
    poller (SqliteEventBroker) to deliver worker-side events to the dashboard
    process's in-memory EventBus.

    id:      Auto-increment PK — used as a monotonic watermark by the poller.
    ts:      Event timestamp (UTC).
    source:  Unique instance-id of the publishing process (e.g. "proc-1234-abc").
             The dashboard poller skips its own source to avoid double-publishing.
    payload: The full event dict as-is (same structure as MemoryEventBroker events).
    """
    __tablename__ = "events"

    id:      Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts:      Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    source:  Mapped[str]      = mapped_column(String, default="", index=True)
    payload: Mapped[dict]     = mapped_column(JSON, nullable=False)


class ApprovalRow(Base):
    """One row per structured approval request (IEventBroker.request_approval).

    The requesting process writes status="pending" and blocks-polls.
    The dashboard UI resolves it by writing status="resolved" + choice.
    Timed-out requests are marked status="timed_out" by the requester.
    """
    __tablename__ = "approvals"

    request_id:  Mapped[str]           = mapped_column(String, primary_key=True)
    title:       Mapped[str]           = mapped_column(String, default="")
    message:     Mapped[str]           = mapped_column(Text, default="")
    options:     Mapped[list]          = mapped_column(JSON, default=list)
    context:     Mapped[dict]          = mapped_column(JSON, default=dict)
    # "pending" | "resolved" | "timed_out"
    status:      Mapped[str]           = mapped_column(String, default="pending", index=True)
    choice:      Mapped[str|None]      = mapped_column(String, nullable=True)
    default:     Mapped[str]           = mapped_column(String, default="")
    timeout_s:   Mapped[float]         = mapped_column(Float, default=120.0)
    created_at:  Mapped[datetime]      = mapped_column(DateTime, default=func.now())
    resolved_at: Mapped[datetime|None] = mapped_column(DateTime, nullable=True)


# ─────────────────────────────────────────────────────────────────────────────
#  A.2 — Durable supervisor queue
# ─────────────────────────────────────────────────────────────────────────────

class SupervisorQueueRow(Base):
    """One row per queued or in-flight supervisor task.

    States: queued → running → completed | failed | dead_letter
    Soft-lease: a row stuck in 'running' beyond the lease threshold is
    reclaimable by a new worker instance (handles crashes without data loss).

    The composite index (state, priority, enqueued_at) is declared in
    __table_args__ so SQLAlchemy creates it via create_all() — it supports
    FIFO-with-priority dequeue in a single indexed scan.
    """
    __tablename__ = "supervisor_queue"
    __table_args__ = (
        # Primary dequeue index: queued rows in priority + FIFO order
        __import__("sqlalchemy", fromlist=["Index"]).Index(
            "ix_supervisor_queue_dequeue",
            "state", "priority", "enqueued_at",
        ),
    )

    submission_id:     Mapped[str]           = mapped_column(String, primary_key=True)
    activity_id:       Mapped[str]           = mapped_column(String, nullable=False, index=True)
    shadow_id:         Mapped[str]           = mapped_column(String, nullable=False)
    priority:          Mapped[int]           = mapped_column(Integer, default=5)
    retry_count:       Mapped[int]           = mapped_column(Integer, default=0)
    reason:            Mapped[str]           = mapped_column(String, default="")
    enqueued_at:       Mapped[float]         = mapped_column(Float, nullable=False)
    # state: "queued" | "running" | "completed" | "failed" | "dead_letter"
    state:             Mapped[str]           = mapped_column(String, default="queued", index=True)
    claimed_by:        Mapped[str|None]      = mapped_column(String, nullable=True)   # worker id
    claimed_at:        Mapped[float|None]    = mapped_column(Float, nullable=True)
    last_heartbeat_at: Mapped[float|None]    = mapped_column(Float, nullable=True)
    attempt_count:     Mapped[int]           = mapped_column(Integer, default=0)
    result_json:       Mapped[str|None]      = mapped_column(Text, nullable=True)     # JSON outcome
    error_text:        Mapped[str|None]      = mapped_column(Text, nullable=True)


# ─────────────────────────────────────────────────────────────────────────────
#  — Operator Recovery Path: tool-dependency approvals
# ─────────────────────────────────────────────────────────────────────────────

class ToolDepApproval(Base):
    """Operator-approved tool runtime dependencies.

    When a forged tool declares a third-party dependency (e.g. ``requests``),
    the operator must explicitly approve the package before it can be installed
    into the sandbox / baked into the worker image.  This table is the
    source-of-truth that the dependency-approval wizard writes to and that
    the image-bake pipeline reads from.
    """
    __tablename__ = "tool_dep_approvals"

    id:                   Mapped[str]      = mapped_column(String, primary_key=True)
    package_name:         Mapped[str]      = mapped_column(String, nullable=False, index=True)
    package_version_spec: Mapped[str|None] = mapped_column(String, nullable=True)
    approved_by:          Mapped[str|None] = mapped_column(String, nullable=True)
    approved_at:          Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source:               Mapped[str]      = mapped_column(String, nullable=False, default="dashboard")
    baked_in_image:       Mapped[bool]     = mapped_column(Boolean, nullable=False, default=False)
