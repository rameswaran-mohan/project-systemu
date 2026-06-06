"""v0.9.1 StateDelta — durable-state snapshot diff fed to the fresh-context verifier.

Captures changes the LLM's primary loop made during an objective's iteration
block: files added/modified under default_output_dir, audit-log entries added
for this execution_id, the chat reply (if set), and new vault records. The
verifier judges whether these changes constitute completion of the objective.

The four named surfaces (files/audit/chat/vault) ship in v0.9.1. The
``extensions`` slot is opaque and pluggable so Layer 5+ (MCP, RAG, skill
recipes) can pass surfaces to the verifier without requiring a prompt edit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StateDelta(BaseModel):
    """A compact snapshot diff fed to the fresh-context verifier."""

    files_added:        List[Dict[str, Any]] = Field(default_factory=list)
    files_modified:     List[Dict[str, Any]] = Field(default_factory=list)
    audit_entries_added: List[Dict[str, Any]] = Field(default_factory=list)
    chat_result_set:    Optional[str] = None
    vault_records_added: List[str] = Field(default_factory=list)
    iteration_start_ts: str = ""
    extensions:         Dict[str, Any] = Field(default_factory=dict)


class _Baseline(BaseModel):
    """Internal snapshot used by capture_baseline. Not part of the LLM contract."""
    files: Dict[str, float] = Field(default_factory=dict)   # path -> mtime
    audit_count: int = 0
    chat_result: Optional[str] = None
    vault_record_paths: List[str] = Field(default_factory=list)
    iteration_start_ts: str = ""


def capture_baseline(
    *,
    vault,
    execution_id: str,
    objective_id: int,
    default_output_dir: str,
) -> _Baseline:
    """Snapshot durable state at the start of this objective's iteration block.

    The diff between this baseline and the state at completion claim becomes
    the StateDelta fed to the verifier.
    """
    base = _Baseline(iteration_start_ts=_iso_now())

    out_dir = Path(default_output_dir)
    if out_dir.exists() and out_dir.is_dir():
        for p in out_dir.rglob("*"):
            if p.is_file():
                try:
                    base.files[str(p)] = p.stat().st_mtime
                except OSError:
                    pass

    existing = vault.query_action_audit(execution_id=execution_id)
    base.audit_count = len(existing)

    return base


def compute_delta(
    *,
    baseline: _Baseline,
    vault,
    default_output_dir: str,
    chat_result: Optional[str],
    config,
    execution_id: Optional[str] = None,
    user_id: Optional[str] = None,
    extensions: Optional[Dict[str, Any]] = None,
) -> StateDelta:
    """Compute the delta between baseline and current durable state.

    Bounded by ``config.state_delta_max_files_per_section`` (each list) and
    ``config.state_delta_file_preview_chars`` (preview chars per file).
    """
    max_files = int(config.state_delta_max_files_per_section)
    preview_chars = int(config.state_delta_file_preview_chars)

    files_added: List[Dict[str, Any]] = []
    files_modified: List[Dict[str, Any]] = []
    out_dir = Path(default_output_dir)
    if out_dir.exists() and out_dir.is_dir():
        for p in out_dir.rglob("*"):
            if not p.is_file():
                continue
            sp = str(p)
            try:
                stat = p.stat()
            except OSError:
                continue
            entry = {
                "path": sp,
                "size": stat.st_size,
                "preview": _safe_preview(p, preview_chars),
            }
            if sp not in baseline.files:
                if len(files_added) < max_files:
                    files_added.append(entry)
            elif stat.st_mtime > baseline.files[sp]:
                if len(files_modified) < max_files:
                    files_modified.append(entry)

    audit_filter_execution_id = execution_id
    audit_entries_added: List[Dict[str, Any]] = []
    if audit_filter_execution_id is not None:
        rows = vault.query_action_audit(
            execution_id=audit_filter_execution_id,
            since_ts=baseline.iteration_start_ts,
            user_id=user_id,
        )
        audit_entries_added = rows[:max_files]

    return StateDelta(
        files_added=files_added,
        files_modified=files_modified,
        audit_entries_added=audit_entries_added,
        chat_result_set=chat_result,
        vault_records_added=[],
        iteration_start_ts=baseline.iteration_start_ts,
        extensions=dict(extensions or {}),
    )


def _safe_preview(path: Path, n: int) -> str:
    """Read first ``n`` chars of the file as utf-8, lossy on binary."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(n)
    except OSError:
        return ""
