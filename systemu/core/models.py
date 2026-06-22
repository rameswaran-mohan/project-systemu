"""Pydantic data models for all Systemu entities.

Entities:
  Scroll       — refined, structured version of a captured SOP
  ActionBlock  — a single deterministic step within a Scroll
  Tool         — a callable capability available to Shadows
  Skill        — an abstract proficiency demonstrated via Scrolls
  Activity     — bundles a Scroll with its required Skills + Tools
  Shadow       — an autonomous agent persona assigned activities
  Evolution    — a proposed improvement to any vault entity
  Notification — a pending user decision (approve/reject)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

import re

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


# v0.6.1-a: tool names become filenames on disk (impl_dir / f"{name}.py").  An
# LLM-supplied name with ../ or / would escape impl_dir.  This regex is the
# single source of truth — used by the Tool.name validator AND by the backstop
# check in systemu/pipelines/tool_forge.py.
_SAFE_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# v0.8.18: credential keys become env-var-style identifiers (e.g. OPENWEATHER_API_KEY).
_SAFE_CRED_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

from systemu.core.utils import utcnow as _now


# ─────────────────────────────────────────────────────────────────────────────
#  v0.8.16: Origin taxonomy — canonical trigger origin for every event
# ─────────────────────────────────────────────────────────────────────────────

ORIGINS = {"chat", "capture", "manual", "scheduled", "system"}

_REASON_TO_ORIGIN = {
    "chat": "chat", "ui-submit": "chat",
    "manual": "manual",
    "scheduled": "scheduled",
    "capture": "capture",
    "restart-restore": "system", "crash-recovery": "system", "db-restore": "system",
    "startup_recovery_assigned": "system",
}


def coerce_origin(reason) -> str:
    """Map a submit `reason` (or raw origin) to a canonical event origin.
    Exact origin → itself; known reason → mapped; retry-* → system; unknown/empty → manual."""
    if reason in ORIGINS:
        return reason
    key = str(reason or "").strip().lower()
    if key in _REASON_TO_ORIGIN:
        return _REASON_TO_ORIGIN[key]
    if key.startswith("retry") or key.startswith("operator_") or "recovery" in key or "restore" in key:
        return "system"
    return "manual"


# ─────────────────────────────────────────────────────────────────────────────
#  Scroll
# ─────────────────────────────────────────────────────────────────────────────

class ScrollStatus(str, Enum):
    DRAFT             = "draft"
    REFINED           = "refined"
    PENDING_APPROVAL  = "pending_approval"   # Awaiting user approval
    APPROVED          = "approved"           # User approved (or auto-approved)
    ACTIVE            = "active"             # Activity extracted; shadow assignment in progress
    LINKED            = "linked"             # Activity extracted AND shadow assigned
    EVOLVED           = "evolved"            # Evolution has been applied
    VALIDATOR_BLOCKED = "validator_blocked"  # v0.6.5-d: Stage 6 validator found a blocker
    EXTRACTION_FAILED = "extraction_failed"  # v0.8.13: re-extraction repeatedly failed — terminal


class TraceEvent(BaseModel):
    """v0.6.5-a: Per-stage pipeline observation appended to Scroll.pipeline_trace.

    Each pipeline stage (intent/refine/extract/validate/deprecate) appends a
    TraceEvent describing what it decided and why.  Surfaced on /scrolls as
    a yellow warning badge + Pipeline Trace panel in the detail view.
    """
    stage:   Literal["intent", "refine", "extract", "validate", "deprecate"]
    level:   Literal["info", "warn", "error"]
    message: str
    detail:  Dict[str, Any] = Field(default_factory=dict)
    ts:      datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class ActionBlock(BaseModel):
    """A single deterministic, machine-executable step within a Scroll (legacy format)."""

    step_number:      int
    action:           str               # navigate | click | type | run_command | open_file | …
    target:           str               # URL, selector, file path, command string
    parameters:       Dict[str, Any] = {}
    expected_outcome: str = ""
    application:      str = ""          # e.g. "Google Chrome", "VS Code"


class Objective(BaseModel):
    """A verifiable sub-goal the Shadow must achieve (intent-driven format)."""

    id:               int
    goal:             str               # imperative statement of what to accomplish
    success_criteria: str               # verifiable condition proving completion
    output_type:      str = ""          # file | data | state_change | side_effect
    hints:            Dict[str, Any] = {}   # observed details: urls, paths, formats, naming
    depends_on:       List[int] = []    # objective IDs that must complete first
    # v0.9.1 (Layer 4): free-text description of what durable evidence proves
    # this objective complete. Tier-1 LLM generates this during scroll
    # refinement; runtime hands it to a fresh-context Tier-1 verifier along
    # with a StateDelta to judge whether the work happened. None = legacy
    # behavior (credit on tool success).
    verifier:         Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
#  ScrollParameter  (v0.9.35 — generalization toggle)
# ─────────────────────────────────────────────────────────────────────────────

class ScrollParameter(BaseModel):
    """v0.9.35: a captured specific promoted to an operator-asked PARAMETER.

    Mirrors the elicitation field descriptor so it feeds the existing form
    rail unchanged (see elicitation.elicitation_schema_from_fields). Produced
    when a scroll is recorded with generalization="broad": the abstracted
    intent keeps the WHAT, while each salient specific becomes a slot the
    operator is asked about at execution time. ``default`` carries THE CAPTURED
    VALUE, shown to the operator as an editable suggested default.
    """
    name:         str
    description:  str = ""
    type:         Literal["string", "number", "integer", "boolean"] = "string"
    # THE CAPTURED VALUE — shown to the operator as an editable suggested default.
    default:      Any = None
    # Coarse class of the specific, e.g. "product","site","path","date","value".
    salient_kind: Optional[str] = None
    enum:         Optional[List[str]] = None
    format:       Optional[str] = None
    required:     bool = True


class Scroll(BaseModel):
    """A refined, AI-understandable Standard Operating Procedure."""

    id:                    str
    name:                  str
    source_session_id:     str
    raw_instructions_path: str
    narrative_md:          str                    # human-readable prose version

    # ── Intent-driven fields (new scrolls) ────────────────────────────────────
    intent:               str = ""               # 1-2 sentence overall goal — the WHY
    # v0.6.0-c: concrete success description distinct from intent.  Where
    # `intent` is "why" (the outcome the user wants), `expected_outcome` is
    # "what success looks like" in observable terms (artifacts created,
    # state changed).  Read by Stage 5 (shadow tiebreak) and Stage 6 (validator).
    expected_outcome:     str = ""
    objectives:           List[Objective] = []   # decomposed verifiable sub-goals
    constraints:          Dict[str, Any] = {}    # output format, naming, locations
    observed_preferences: Dict[str, Any] = {}    # date formats, tool choices, conventions

    # ── Legacy fields (kept for backward compatibility) ───────────────────────
    action_blocks:         List[ActionBlock] = []  # GUI mimicry steps — empty for new scrolls

    activity_id:           Optional[str] = None
    status:                ScrollStatus = ScrollStatus.DRAFT
    version:               int = 1
    recovery_attempts:     int = 0           # v0.8.13: bounded re-extraction retry counter
    tags:                  List[str] = []
    created_at:            datetime = Field(default_factory=_now)
    updated_at:            datetime = Field(default_factory=_now)

    # v0.6.5-a: pipeline observability — each stage appends a TraceEvent.
    # Read by /scrolls UI for the warning badge + Pipeline Trace panel.
    pipeline_trace:        List[TraceEvent] = Field(default_factory=list)

    # v0.9.7 (Phase 3.3): verbatim user message that originated this scroll.
    # Decision 0.1 #2: the authoritative GOAL is the raw user text, not the
    # refiner's restatement. The runtime goal-verifier reads this field to
    # check work against the original intent rather than the LLM paraphrase.
    # None for scrolls created before v0.9.7 or from non-chat origins.
    raw_request:           Optional[str] = None

    # v0.9.7 (Phase 3.3): per-SOP adherence level chosen at save time.
    # Controls how strictly the runtime follows the SOP vs. allowing
    # contextual adaptation. Values: "free" | "guided" | "strict" | None.
    # None means "use system default" (backward-compatible with pre-v0.9.7
    # scrolls). Set by the save-SOP UI/CLI when a recording is promoted to a
    # reusable SOP.
    adherence:             Optional[str] = None

    # v0.9.35 (generalization toggle): record-time choice of how abstract the
    # recorded intent is and which captured specifics become operator-asked
    # PARAMETERS at execution. None == "standard" == today's behaviour — chosen
    # at record time, finalized by the scroll refiner. Additive + JSON-blob
    # persisted via model_dump (same as adherence/raw_request above) — no DB
    # migration.
    generalization:        Optional[Literal["broad", "standard", "narrow"]] = None
    # Salient specifics promoted to operator-asked parameters (broad mode).
    # Empty for standard/narrow scrolls and all pre-v0.9.35 scrolls.
    parameters:            List[ScrollParameter] = []

    @computed_field
    @property
    def has_warnings(self) -> bool:
        """v0.6.5-a: True when any trace event has level in {warn, error}."""
        return any(e.level in ("warn", "error") for e in self.pipeline_trace)


# ─────────────────────────────────────────────────────────────────────────────
#  Tool
# ─────────────────────────────────────────────────────────────────────────────

class ToolType(str, Enum):
    PYTHON_FUNCTION = "python_function"
    CLI_COMMAND     = "cli_command"
    BROWSER_ACTION  = "browser_action"
    API_CALL        = "api_call"
    FILE_OPERATION  = "file_operation"


_TOOL_TYPE_SYNONYMS = {
    "web": ToolType.API_CALL, "web_fetch": ToolType.API_CALL, "http": ToolType.API_CALL,
    "https": ToolType.API_CALL, "url": ToolType.API_CALL, "rest": ToolType.API_CALL,
    "fetch": ToolType.API_CALL, "request": ToolType.API_CALL, "download": ToolType.API_CALL,
    "scrape": ToolType.BROWSER_ACTION, "scraping": ToolType.BROWSER_ACTION,
    "browser": ToolType.BROWSER_ACTION, "render": ToolType.BROWSER_ACTION,
    "screen_capture": ToolType.BROWSER_ACTION, "screenshot": ToolType.BROWSER_ACTION,
    "screen": ToolType.BROWSER_ACTION,
    "shell": ToolType.CLI_COMMAND, "command": ToolType.CLI_COMMAND, "bash": ToolType.CLI_COMMAND,
    "file": ToolType.FILE_OPERATION, "filesystem": ToolType.FILE_OPERATION, "io": ToolType.FILE_OPERATION,
    "function": ToolType.PYTHON_FUNCTION, "code": ToolType.PYTHON_FUNCTION, "python": ToolType.PYTHON_FUNCTION,
}


def coerce_tool_type(raw, *, default: "ToolType" = ToolType.PYTHON_FUNCTION) -> "ToolType":
    """Map any value to a valid ToolType. Never raises.

    Exact enum value/member -> itself; known synonym -> mapped; unknown/empty/None -> default.
    Logs at DEBUG when it coerces a non-exact string so we can see what the model emits.
    """
    if isinstance(raw, ToolType):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return default
    key = raw.strip().lower()
    try:
        return ToolType(key)               # exact value match
    except ValueError:
        mapped = _TOOL_TYPE_SYNONYMS.get(key, default)
        logger.debug("[models] coerce_tool_type: %r -> %s", raw, mapped.value)
        return mapped


class ToolStatus(str, Enum):
    PROPOSED = "proposed"   # Identified but not yet implemented
    FORGED   = "forged"     # Code generated, pending test
    TESTED   = "tested"     # Dry-run passed
    DEPLOYED = "deployed"   # Available for Shadow use
    UPGRADED = "upgraded"   # Evolution improved it


class CredentialRequirement(BaseModel):
    """A credential a tool needs to run (v0.8.18)."""
    key:         str                                    # env-var-style id, e.g. "OPENWEATHER_API_KEY"
    label:       str                                    # human label
    auth_type:   Literal["none", "api_key"] = "api_key" # "oauth" reserved for a follow-up
    signup_url:  Optional[str] = None
    free_tier:   bool = False
    description: str = ""

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        if not isinstance(v, str) or not _SAFE_CRED_KEY.match(v):
            raise ValueError(f"CredentialRequirement.key must match ^[A-Z][A-Z0-9_]{{1,63}}$ (got {v!r}).")
        return v


class Tool(BaseModel):
    """A callable capability registered in the vault tool registry."""

    id:                  str
    name:                str
    description:         str
    tool_type:           ToolType
    parameters_schema:   Dict[str, Any] = {}    # JSON Schema describing inputs
    requires_credentials: List["CredentialRequirement"] = []   # v0.8.18: declared credential needs

    # v0.6.1-a: validate Tool.name at construction so unsafe values can never
    # reach the filesystem.  See _SAFE_TOOL_NAME at the top of this module.
    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not isinstance(v, str) or not _SAFE_TOOL_NAME.match(v):
            raise ValueError(
                f"Tool.name must match ^[a-z][a-z0-9_]{{0,63}}$ "
                f"(got {v!r}).  Reject to prevent path traversal during forge."
            )
        return v

    @field_validator("tool_type", mode="before")
    @classmethod
    def _coerce_tool_type(cls, v):
        return coerce_tool_type(v)

    return_schema:       Dict[str, Any] = {}    # JSON Schema describing output
    implementation_notes: str = ""              # Library choices, approach, API hints for code gen
    dependencies:        List[str] = []         # pip packages required (e.g. ["playwright"])
    implementation_path: str = ""               # relative to vault/tools/implementations/
    tool_md_path:        str = ""               # path to TOOL.md manifest
    status:              ToolStatus = ToolStatus.PROPOSED
    forged_by_systemu:   bool = False
    # v0.10.0 — the execution that runtime-forged this tool (provenance for the
    # "agent-built" view + the harness ledger). None for design-time/operator forges.
    forged_by_execution_id: Optional[str] = None
    # W2.2: forged (LLM-generated) tools execute in a subprocess by default;
    # the operator may opt a reviewed tool back into the in-process fast path
    # (~100-500ms faster per call) by setting this. Built-ins (not forged)
    # always keep the fast path.
    trusted_inprocess:   bool = False
    enabled:             bool = False   # Must be explicitly toggled ON by user after code review
    version:             int = 1
    # v0.5.0-a: dry-run validation gate (Gate 3.5).
    # Tools cannot be enabled until dry_run_status == "passed".
    # ``dry_run_evidence`` stores the last attempt's outcome (params used,
    # error, elapsed_ms) for operator inspection on the Tools page.
    dry_run_status:      str = "not_run"   # not_run | passed | failed | skipped
    dry_run_evidence:    Dict[str, Any] = {}
    # v0.5.0-a: rolling buffer of observed-successful param sets, capped
    # at 20 entries.  Used by v0.5.0-d's backward-compat replay when the
    # supervisor wants to bump the tool's version — replays each entry
    # against the new code to prove no regression for known-working uses.
    last_successful_params: List[Dict[str, Any]] = []
    # v0.5.0-b: append-only audit of recalibration events.  Each entry:
    #   {"version": int, "reason": str, "mode": "bump"|"fork",
    #    "diff_summary": str, "ts": iso}
    evolution_history:   List[Dict[str, Any]] = []
    created_at:          datetime = Field(default_factory=_now)
    updated_at:          datetime = Field(default_factory=_now)

    # v0.9.1 (Layer 4): durable-action audit gate. When True, tool_sandbox
    # writes one entry to vault/audit/actions.jsonl on each successful
    # invocation. Read tools stay False; action tools (chat_submit,
    # write_csv_file, email.send, etc.) opt in.
    is_action_tool: bool = False

    # v0.9.1 rev 4 (L3-readiness slot): toolset membership for the future
    # code-side tool-registry layer (ToolEntry). No enforcement in
    # v0.9.1 — slot reserved so v0.9.3 doesn't need a model migration.
    # Examples (post-L3): "file", "vault", "web", "memory", "delegate",
    # "session", "skill", "clarify", "time", "chat".
    toolset: Optional[str] = None

    # v0.9.1 rev 4: per-tool output cap, in characters. None = no cap
    # (current behavior). tool_sandbox truncates ToolResult.stdout to this
    # bound and logs at DEBUG when truncation fires. Prevents runaway shell
    # output from blowing the LLM context window.
    max_result_size_chars: Optional[int] = None

    # v0.9.1.1 hotfix: per-tool wall-clock budget, in seconds. None = use
    # config.tool_default_timeout_seconds. Honored by tool_registry.execute.
    # Web tools should set ~90s; quick file tools can leave it None.
    timeout_seconds: Optional[int] = None

    # v0.9.3 (Layer 3): name of a check_fn for vault-stored tools. The
    # runtime looks up the actual callable from a name->fn registry at
    # invocation time. Code-registered v2 tools pass the callable directly
    # to registry.register(check_fn=...); this slot is only for v1
    # vault-stored tools to opt into the same availability gating.
    check_fn_name: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
#  Skill  (Agent Skills Standard)
# ─────────────────────────────────────────────────────────────────────────────

class Skill(BaseModel):
    """An abstract proficiency demonstrated by one or more Scrolls.

    Follows the Anthropic Agent Skills open standard:
      vault/skills/skill_<id>/
        SKILL.md          -- YAML frontmatter + procedural instructions body
        scripts/          -- optional executable automation scripts

    Skills encode *procedural knowledge* (how to do X), NOT executable logic.
    Tools are the execution layer; Skills are the expertise layer.
    """

    id:                  str
    name:                str
    description:         str
    category:            str = ""               # browser | file_ops | devops | data | …
    proficiency_level:   str = "intermediate"   # beginner | intermediate | expert
    evidence_scroll_ids: List[str] = []         # scrolls that demonstrate this skill
    required_tool_ids:   List[str] = []         # internal vault IDs for linking
    required_tool_names: List[str] = []         # human-readable names for SKILL.md frontmatter
    instructions_md:     str = ""               # the how-to procedural body (SKILL.md body)
    skill_md_path:       str = ""               # path to SKILL.md
    # ── v0.6.0-d.5: intent contract + runtime telemetry ─────────────────────
    # These fields live ONLY in the internal vault JSON + SQLite columns.  The
    # portable SKILL.md export (Anthropic Agent Skills Standard) does NOT
    # include them — see plan §"Anthropic Agent Skills Standard compliance".
    target_outcomes:     List[str] = []         # intent components this skill serves
    produces:            List[str] = []         # data | structured_document | image | side_effect | report | data_extraction
    effectiveness_score: float = 1.0            # decays on downstream failure; recal trigger at < 0.5
    skill_version:       int = 1                # bumps on RECALIBRATE_SKILL (mirrors Tool.version)
    evolution_history:   List[Dict[str, Any]] = []   # append-only audit of recalibrations
    created_at:          datetime = Field(default_factory=_now)
    updated_at:          datetime = Field(default_factory=_now)


# ─────────────────────────────────────────────────────────────────────────────
#  SessionSummary  (v0.9.2 — Layer 2 Episodic Memory)
# ─────────────────────────────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    """One persistent record per completed run, written by episodic_memory.

    Tier-1 generates ``outcome_summary``, ``key_facts_learned``, and ``tags``
    at run-end. The search index (FTS5 in sqlite, tsvector in postgres) is
    built over ``intent + outcome_summary + tags`` so the LLM can ask
    "have I done this before?" across sessions.
    """
    id:                  str
    session_id:          str
    execution_id:        Optional[str] = None
    user_id:             Optional[str] = None
    started_at:          datetime
    completed_at:        datetime
    status:              str               # success | partial | failed | stuck
    intent:              str
    outcome_summary:     str
    key_facts_learned:   List[str] = Field(default_factory=list)
    files_produced:      List[str] = Field(default_factory=list)
    tags:                List[str] = Field(default_factory=list)
    raw_chat_id:         Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
#  Activity
# ─────────────────────────────────────────────────────────────────────────────

class ActivityStatus(str, Enum):
    UNASSIGNED  = "unassigned"   # No shadow assigned yet
    PARTIAL     = "partial"      # Some required tools are still PROPOSED
    ASSIGNED    = "assigned"     # Shadow assigned, ready to execute
    EXECUTABLE  = "executable"   # All tools deployed, can run immediately
    COMPLETED   = "completed"    # Shadow execution succeeded — terminal state
    FAILED      = "failed"       # Terminal failure — retries exhausted / structural blocker
    CANCELLED   = "cancelled"    # v0.9.32: operator-interrupted — terminal, no post-mortem


class Activity(BaseModel):
    """Bundles a Scroll with its extracted Skills and Tools."""

    id:                  str
    name:                str
    scroll_id:           str
    required_tool_ids:   List[str] = []
    required_skill_ids:  List[str] = []
    missing_tools:       List[str] = []    # tool names not yet forged
    assigned_shadow_id:  Optional[str] = None
    status:              ActivityStatus = ActivityStatus.UNASSIGNED
    origin:              str = "manual"   # v0.8.16: trigger origin {chat,capture,manual,scheduled,system}
    # v0.6.0-f: Frozen intent at extraction time so Stage 5 (shadow tiebreak)
    # can do semantic matching without re-loading the scroll on every call.
    intent_snapshot:     str = ""
    created_at:          datetime = Field(default_factory=_now)
    updated_at:          datetime = Field(default_factory=_now)


# ─────────────────────────────────────────────────────────────────────────────
#  Shadow
# ─────────────────────────────────────────────────────────────────────────────

class ShadowStatus(str, Enum):
    DORMANT  = "dormant"    # Created but no activities yet
    AWAKENED = "awakened"   # Activities assigned, ready
    ACTIVE   = "active"     # Currently executing a task
    EVOLVED  = "evolved"    # Evolution applied
    RETIRED  = "retired"    # Merged or superseded


class Shadow(BaseModel):
    """An autonomous agent persona that executes Activities via Scrolls.

    Identity tier (v0.3+) — split into two fields:

    * :attr:`identity_block` — operator-editable in Workshop.  The contract
      for what the Shadow *is* (name, role, expertise scope, communication
      style, hard constraints).  Limit ~500 tokens.
    * :attr:`accumulated_voice` — consolidator-grown, append-only with
      rotation.  Traits the Shadow has demonstrated across executions
      (verbal patterns, decision-making style, recurring fallbacks).
      The Shadow can read this but cannot write to it; the consolidator
      owns the writes.

    The runtime ``system_prompt`` sent to the LLM is composed from both
    fields by :attr:`system_prompt` — a computed field that's both
    backwards-compatible (legacy callers reading ``shadow.system_prompt``
    keep working) and forward-safe (new callers can edit
    ``identity_block`` independently).

    Legacy migration: when a Shadow is loaded from JSON that has only the
    old ``system_prompt`` field (pre-v0.3), the model validator copies
    the value into ``identity_block`` and leaves ``accumulated_voice``
    empty.  Save-time then writes the v0.3 shape; the legacy field
    disappears from new files.  No data loss.
    """

    id:                   str
    name:                 str
    description:          str

    # Identity tier (v0.3) — operator-editable + consolidator-grown.
    identity_block:       str = ""           # operator-controlled
    accumulated_voice:    str = ""           # consolidator-grown

    assigned_activity_ids: List[str] = []
    available_tool_ids:   List[str] = []
    skill_ids:            List[str] = []
    status:               ShadowStatus = ShadowStatus.DORMANT
    execution_log:        List[Dict[str, Any]] = []
    evolution_history:    List[Dict[str, Any]] = []
    memory_md_path:       str = ""           # path to SHADOW_MEMORY.md
    memory_buffer_path:   str = ""           # path to memory_buffer.jsonl
    # v0.4.1: per-shadow opt-in for the Intelligent Supervisor (v0.4.0).
    # The Supervisor activates when either this OR
    # ``config.intelligent_supervisor_enabled`` is True — lets the operator
    # A/B test on one shadow before flipping the global switch.
    supervisor_enabled:   bool = False
    # v0.4.3-b: operator-labelled specialty for routing preference.
    # Free-form short tag (e.g. "browser", "data-pipeline", "devops").
    # When set, ``Supervisor._resolve_shadow_with_affinity`` prefers
    # candidates whose specialty matches the originating shadow's
    # specialty (best signal we have for "the same kind of work" when
    # no shadow-metric history exists yet).
    specialty:            str = ""
    created_at:           datetime = Field(default_factory=_now)
    updated_at:           datetime = Field(default_factory=_now)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_system_prompt(cls, data: Any) -> Any:
        """Pre-v0.3 shadow.json had a single ``system_prompt`` field.

        Migrate it transparently: when ``identity_block`` is absent or
        empty but ``system_prompt`` is set, treat the legacy value as
        the operator-controlled identity block.  ``accumulated_voice``
        remains empty until the consolidator's first run on this Shadow.
        """
        if not isinstance(data, dict):
            return data
        legacy = data.get("system_prompt")
        if legacy and not data.get("identity_block"):
            data["identity_block"] = legacy
        # Drop the legacy field so it doesn't get re-serialised; the
        # computed_field below will reconstitute it on demand.
        data.pop("system_prompt", None)
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def system_prompt(self) -> str:
        """Runtime persona prompt — composed from identity + voice.

        Callers reading ``shadow.system_prompt`` (the existing read path
        in ``shadow_runtime``, the dashboard, etc.) continue to work
        unchanged.  Computed at access time so any update to
        ``identity_block`` or ``accumulated_voice`` is reflected on the
        next read.
        """
        identity = (self.identity_block or "").strip()
        voice    = (self.accumulated_voice or "").strip()
        if not voice:
            return identity
        if not identity:
            return voice
        return f"{identity}\n\n{voice}"


# ─────────────────────────────────────────────────────────────────────────────
#  Shadow Memory  (Semantic, self-reflected, evolving)
# ─────────────────────────────────────────────────────────────────────────────

class MemoryCategory(str, Enum):
    HEURISTIC        = "heuristics"
    FAILURE_PATTERN  = "failure_patterns"
    TOOL_QUIRK       = "tool_quirks"
    DOMAIN_GLOSSARY  = "domain_glossary"
    SELF_ASSESSMENT  = "self_assessment"


class MemoryEntry(BaseModel):
    """A single semantic memory entry parsed from SHADOW_MEMORY.md.

    The MD file is the source of truth; this dataclass exists for in-memory
    consolidation and relevance scoring. It is never persisted as JSON.
    """

    category:      str             # one of MemoryCategory values
    lesson:        str             # the actual content
    confidence:    int = 1         # bumped each time the lesson is re-validated
    last_used_at:  str = ""        # ISO timestamp; refreshed on use
    evidence_ids:  List[str] = []  # exec_ids that produced or reinforced it
    created_at:    str = ""        # ISO timestamp of first observation


# ─────────────────────────────────────────────────────────────────────────────
#  Evolution
# ─────────────────────────────────────────────────────────────────────────────

class EvolutionType(str, Enum):
    MERGE    = "merge"      # Combine two entities
    SPLIT    = "split"      # Specialise one into many
    UPGRADE  = "upgrade"    # Improve a single entity
    COMBINE  = "combine"    # Merge scrolls into a workflow
    DISCOVER = "discover"   # New skill / pattern found


class EvolutionStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED  = "applied"


class Evolution(BaseModel):
    """A proposed improvement to one or more vault entities."""

    id:                  str
    evolution_type:      EvolutionType
    target_entity_type:  str               # "shadow" | "tool" | "scroll" | "skill"
    target_entity_ids:   List[str]
    description:         str
    rationale:           str
    before_snapshot:     Dict[str, Any] = {}
    after_snapshot:      Dict[str, Any] = {}
    status:              EvolutionStatus = EvolutionStatus.PROPOSED
    proposed_at:         datetime = Field(default_factory=_now)
    resolved_at:         Optional[datetime] = None

    # ── Workshop edit provenance (Phase 1) ────────────────────────────────────
    edit_classification: Optional[str] = None   # "metadata" | "behavior" | "contract"
    fields_changed:      List[str] = []
    reverted:            bool = False


# ─────────────────────────────────────────────────────────────────────────────
#  Notification
# ─────────────────────────────────────────────────────────────────────────────

class NotificationStatus(str, Enum):
    PENDING  = "pending"
    RESOLVED = "resolved"
    EXPIRED  = "expired"


class Notification(BaseModel):
    """A pending user decision queued by Systemu."""

    id:          str
    title:       str
    message:     str
    actions:     List[str]              # e.g. ["Approve", "Reject"]
    context:     Dict[str, Any] = {}    # arbitrary payload for callback
    status:      NotificationStatus = NotificationStatus.PENDING
    created_at:  datetime = Field(default_factory=_now)
    resolved_at: Optional[datetime] = None
    resolution:  Optional[str] = None  # which action was chosen


# ─────────────────────────────────────────────────────────────────────────────
#  Schedule (v0.8.6)
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleMode(str, Enum):
    ONCE      = "once"
    RECURRING = "recurring"


class ScheduleStatus(str, Enum):
    ACTIVE    = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Schedule(BaseModel):
    """v0.8.6: Operator-created schedule for executing a shadow against a scroll.

    Modes:
      - ONCE:      scheduled_at is the single fire time. status → COMPLETED after firing.
      - RECURRING: scheduled_at is the first fire; next_fire_at advances by
                   interval_minutes after each fire. end_at (optional) caps the
                   recurring sequence.

    Skip-missed semantics: if the dashboard was down at next_fire_at, the
    scheduler fires once on restart and recomputes next_fire_at = now + interval
    (recurring) or marks COMPLETED (once).
    """
    id:               str
    shadow_id:        str
    scroll_id:        str
    mode:             ScheduleMode
    dry_run:          bool = False
    scheduled_at:     datetime
    interval_minutes: Optional[int] = None
    end_at:           Optional[datetime] = None
    next_fire_at:     datetime
    last_fire_at:     Optional[datetime] = None
    status:           ScheduleStatus = ScheduleStatus.ACTIVE
    created_at:       datetime
    created_by:       str = "operator (dashboard)"
    # v0.8.7: missed-fire tracking
    missed:             bool = False
    """ONCE schedules: True if this schedule was skipped due to staleness
       (dashboard down past fire time + threshold). RECURRING: not used."""
    missed_fires_count: int = 0
    """RECURRING schedules: cumulative number of fires skipped due to staleness."""
    last_missed_at:     Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────────────────
# v0.9.0 (Layer 1): User model + persistent context
# ─────────────────────────────────────────────────────────────────────────────

class UserProfile(BaseModel):
    """v0.9.0 (Layer 1): the typed spine of what systemu knows about the user.

    Four fields, intentionally minimal. Everything else lives in user_facts.
    Consumers (scroll_refiner, activity_extractor, shadow_runtime) may rely on
    every field being present and well-typed.
    """
    schema_version: int = 1
    name: str
    location_text: str
    timezone: str
    default_output_dir: str

    model_config = {"extra": "forbid"}


class UserFact(BaseModel):
    """v0.9.0 (Layer 1): one freeform fact about the user.

    Facts accumulate in vault/user_facts.jsonl with full provenance. The
    `superseded_by` field lets consolidation mark stale facts without
    rewriting the log (audit trail preserved).
    """
    id: str
    ts: str
    fact: str
    tags: List[str] = Field(default_factory=list)
    source: str
    source_ref: Optional[str] = None
    confidence: float = 1.0
    superseded_by: Optional[str] = None

    model_config = {"extra": "forbid"}


# ─────────────────────────────────────────────────────────────────────────────
#  Capability  (v0.9.3 — Layer 3 Capability Ledger)
# ─────────────────────────────────────────────────────────────────────────────

class Capability(BaseModel):
    """One row in the capability ledger — tracks what systemu knows it can do.

    Both code-registered tools (v0.9.3 ToolRegistry v2) and auto-forged
    vault tools surface here once they've been seen by the runtime.
    The ledger is consulted by the agent for self-check ("do I know how
    to do this before I promise it?") and by the operator via CLI.
    """
    name:            str
    kind:            str               # "tool" | "skill"
    registered_at:   datetime
    last_used_at:    Optional[datetime] = None
    invocations:     int = 0
    successes:       int = 0
    failures:        int = 0
    last_error:      Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
#  SkillManifest  (v0.9.4 — Layer 5 Recipe Fast-Paths)
# ─────────────────────────────────────────────────────────────────────────────

class SkillManifest(BaseModel):
    """Parsed SKILL.md — a loadable, file-based workflow recipe.

    Distinct from the legacy ``Skill`` model: SkillManifest captures the
    on-disk SKILL.md shape (YAML frontmatter + markdown body), while
    ``Skill`` is the runtime-evolved abstract proficiency the agent tracks
    over time. The skill_loader bridges between them.

    On-disk layout: ``skills/<category>/<name>/SKILL.md`` with YAML
    frontmatter (name, description, version, platforms, prerequisites,
    metadata.tags, metadata.related_skills) and a body with sections
    (## When to Use, ## When NOT to Use, ## Procedure, ## Quick Reference).

    Toolset binding: ``requires_toolsets`` (skill declares what it
    needs) + ``fallback_for_toolsets`` (skill replaces when preferred
    toolset is missing).
    """
    name:                  str
    description:           str
    version:               str
    platforms:             List[str] = Field(default_factory=list)
    tags:                  List[str] = Field(default_factory=list)
    related_skills:        List[str] = Field(default_factory=list)
    prerequisites_commands: List[str] = Field(default_factory=list)
    requires_toolsets:     List[str] = Field(default_factory=list)
    fallback_for_toolsets: List[str] = Field(default_factory=list)
    body:                  str
    source_path:           Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
#  v0.9.7 — Reverse-Harness: runtime, agent-initiated capability provisioning
# ─────────────────────────────────────────────────────────────────────────────

class HarnessKind(str, Enum):
    """Families of capability the executing agent can request at runtime."""
    TOOL     = "tool"       # forge / recalibrate a tool
    SKILL    = "skill"      # procedural knowledge
    ACCESS   = "access"     # read a resource/secret; escalate the sandbox
    COMPUTE  = "compute"    # more iterations / think-budget / spend
    SUBAGENT = "subagent"   # spawn a helper sub-Shadow for a sub-objective
    INPUT    = "input"      # ASK_OPERATOR — request info/decision (not a capability)
    MCP      = "mcp"        # connect to an MCP server at runtime (P3)


class HarnessRequest(BaseModel):
    """A runtime request from the executing agent to provision a capability it
    lacks — the inverse of ``TOOL_CALL`` (the ``REQUEST_HARNESS`` loop verb).

    ``spec`` is a kind-specific payload. For ``TOOL`` it mirrors a forge spec
    (name, parameters_schema, return_schema, implementation_notes); for
    ``INPUT`` it carries ``{"question": ..., "options": [...]}``; for ``MCP``
    it carries an MCP-server connect spec::

        {"server_id": str,           # stable id (also the connections key)
         "transport": "stdio"|"http"|"sse",
         "command": str, "args": [str], "env_keys": [str],   # stdio only
         "url": str,                 # http/sse only
         "auth": {...}|None,         # optional auth hint (URL-mode OAuth = P4)
         "label": str,               # human-facing display name
         "tool_filter": [str]|None}  # optional per-tool opt-in subset
    """
    request_id: str = Field(default_factory=lambda: "hreq_" + uuid.uuid4().hex[:8])
    kind:       HarnessKind
    spec:       Dict[str, Any] = Field(default_factory=dict)
    rationale:  str = ""
    fallback:   str = ""        # what the agent will do if denied
    urgency:    Literal["low", "normal", "high"] = "normal"
    # blocking semantics (spec §7): True → escalate+suspend when not
    # auto-grantable; False → downgrade to deny (the run proceeds without it).
    blocking:   bool = True
    # v0.10.0 — pull-decision instrumentation (observability only)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)   # agent's stated need-confidence
    attempts_before_request: int = 0                          # tool tries before resorting to this
    provenance: Dict[str, Any] = Field(default_factory=dict)  # {tool_attempts:[...], blocked_signals:[...]}
    created_at: datetime = Field(default_factory=_now)
    model_config = {"extra": "forbid"}


class RiskBand(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


class HarnessDecision(str, Enum):
    GRANT    = "grant"
    DENY     = "deny"
    ESCALATE = "escalate"


class HarnessVerdict(BaseModel):
    """The Governor's arbitration result for a HarnessRequest."""
    request_id:   str = ""
    decision:     HarnessDecision
    risk_band:    RiskBand = RiskBand.LOW
    rationale:    str = ""
    # set when a GRANT materialises a scoped capability lease
    lease_id:     Optional[str] = None
    # structured alternatives handed back to the agent on DENY (so it adapts)
    alternatives: List[str] = Field(default_factory=list)
    # v0.10.0 — was this verdict resolved by deterministic policy or the LLM judge?
    # (replaces the [judged_by=llm] rationale string tag; powers the cost-tiering measure)
    decided_by:   Literal["deterministic", "llm"] = "deterministic"
    # v0.10.0 — filled by the terminal-pass reconciliation (pull-decision instrumentation)
    request_outcome: Optional[Literal[
        "granted_used", "granted_unused", "denied_fallback_ok",
        "denied_fallback_failed", "escalate_unresolved",
    ]] = None
    created_at:   datetime = Field(default_factory=_now)
    model_config = {"extra": "forbid"}
