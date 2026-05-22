"""Tool recalibration pipeline (v0.5.0-d).

When the supervisor's diagnosis (v0.5.0-c) says a tool is structurally
inadequate, this module orchestrates the response:

1. **bump_version path** — re-forge the tool's spec + code with the
   failure context, dry-run validate (v0.5.0-a), AND replay against
   the rolling history of observed-successful params
   (``Tool.last_successful_params``).  If ANY historical use case
   regresses, abort the bump and recommend fallback to fork.

2. **fork_new_tool path** — forge a brand-new tool with a distinct
   name + id, dry-run validate (no replay needed; nobody else uses it
   yet).  Original tool stays untouched.

Both paths end by publishing an **operator approval card** via the
v0.3.6 supervisor-flash bus.  The card carries the recalibration mode +
rationale + dry-run evidence + spec diff summary so the operator can
either approve (which enables the new tool + auto-maps to the
originating shadow in v0.5.0-e) or override (e.g. force bump when
supervisor recommended fork).

This module does NOT directly modify shadow ``available_tool_ids`` —
the v0.5.0-e resume pathway does that after the operator approves.
Recalibration is a *proposal*; nothing changes until the operator
confirms.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.core.models import Shadow, Tool
    from systemu.vault.vault import Vault
    from systemu.pipelines.tool_inadequacy_diagnosis import InadequacyDiagnosis

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RecalibrationResult:
    """Outcome of a recalibration attempt — what the operator sees on
    the approval card."""

    success:              bool
    mode:                 str        # "bump_version" | "fork_new_tool" | "aborted"
    original_tool_id:     str
    new_tool_id:          str = ""   # equal to original on bump; new id on fork
    new_tool_name:        str = ""
    dry_run_status:       str = ""
    dry_run_error:        str = ""
    replay_status:        str = ""   # "passed" | "failed" | "n/a" | "skipped"
    replay_error:         str = ""
    rationale:            str = ""
    spec_diff_summary:    str = ""
    forced_fallback:      bool = False   # bump tried, fell back to fork
    error:                Optional[str] = None
    # structured spec diff for the operator's approval card.
    # Populated by _bump_version / _fork_new_tool when both old and new
    # specs are available.  Each entry: {"field": str, "old": str, "new": str}.
    spec_diff:            List[Dict[str, str]] = field(default_factory=list)

    def to_card_context(self) -> Dict[str, Any]:
        """Compact dict surfaced on the operator's approval card."""
        return {
            "mode":                self.mode,
            "original_tool_id":    self.original_tool_id,
            "new_tool_id":         self.new_tool_id,
            "new_tool_name":       self.new_tool_name,
            "dry_run_status":      self.dry_run_status,
            "dry_run_error":       (self.dry_run_error or "")[:300],
            "replay_status":       self.replay_status,
            "replay_error":        (self.replay_error or "")[:300],
            "rationale":           self.rationale,
            "spec_diff_summary":   self.spec_diff_summary,
            "spec_diff":           self.spec_diff,
            "forced_fallback":     self.forced_fallback,
            "error":               self.error,
        }


def compute_spec_diff(
    old_spec: Dict[str, Any],
    new_spec: Dict[str, Any],
    *,
    fields: tuple = ("description", "parameters_schema", "return_schema",
                      "implementation_notes", "dependencies"),
) -> List[Dict[str, str]]:
    """Build a structured field-by-field diff between two tool specs.

    — operator-facing visualisation only; never used for
    correctness checks.  Each entry has the field name + truncated
    old/new values (200 chars each) for compact display on the
    approval card.

    Fields whose values are equal are omitted.
    """
    diff: List[Dict[str, str]] = []
    for field_name in fields:
        old_val = old_spec.get(field_name)
        new_val = new_spec.get(field_name)
        if old_val == new_val:
            continue
        diff.append({
            "field": field_name,
            "old":   _summarise_value(old_val),
            "new":   _summarise_value(new_val),
        })
    return diff


def is_low_risk_recalibration(
    *,
    result: "RecalibrationResult",
    tool: "Tool",
    diagnosis: "InadequacyDiagnosis",
) -> tuple[bool, str]:
    """— classify whether a recalibration is safe to auto-approve.

    Conservative criteria — every one must be True:

    1. Recalibration succeeded (`result.success`).
    2. Mode is `fork_new_tool` — bumps modify a tool other shadows depend
       on, so they always require operator approval.
    3. Dry-run passed (not skipped, not failed).
    4. Tool name doesn't look destructive (delete / send / publish / etc.).
    5. Diagnosis confidence is "high" — operator should review medium /
       low confidence verdicts.
    6. Recalibration was NOT a forced fallback (the supervisor's first
       choice failed; operator should know).

    Returns ``(eligible, reason)``.  When ``eligible=False``, ``reason``
    explains the blocking criterion for the audit log.
    """
    if not result.success:
        return (False, "recalibration did not succeed")
    if result.mode != "fork_new_tool":
        return (False, f"mode={result.mode!r} requires operator approval (only fork is auto-approve eligible)")
    if result.dry_run_status != "passed":
        return (False, f"dry-run status {result.dry_run_status!r} (need 'passed')")
    if result.forced_fallback:
        return (False, "recalibration was a fallback from a failed bump — operator should review")
    # Destructive tool heuristic
    name_lower = (tool.name or "").lower()
    destructive_hints = ("delete", "remove", "drop", "wipe", "purge", "send",
                          "publish", "deploy", "purchase", "pay", "transfer")
    if any(h in name_lower for h in destructive_hints):
        return (False, f"tool name '{tool.name}' looks destructive")
    confidence = (diagnosis.confidence or "low").lower()
    if confidence != "high":
        return (False, f"diagnosis confidence {confidence!r} (need 'high')")
    return (True, "fork-mode with passing dry-run, non-destructive tool, high-confidence diagnosis")


def _summarise_value(v: Any, max_len: int = 200) -> str:
    """Compact textual summary for a spec field value."""
    if v is None:
        return "(none)"
    if isinstance(v, (dict, list)):
        try:
            text = json.dumps(v, ensure_ascii=False, indent=2)
        except Exception:
            text = str(v)
    else:
        text = str(v)
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point

def recalibrate_tool(
    *,
    tool: "Tool",
    shadow: "Shadow",
    diagnosis: "InadequacyDiagnosis",
    failure_context: str,
    config: "Config",
    vault: "Vault",
    execution_id: str = "",
) -> RecalibrationResult:
    """Run the v0.5.0-d recalibration pipeline.

    Args:
        tool:             The inadequate tool from the failing execution.
        shadow:           The shadow that needs the recalibration.
        diagnosis:        The v0.5.0-c verdict (mode + rationale).
        failure_context:  Short string describing what failed (passed
                          to the forge prompt as ``prior_dry_run_failure``).
        config:           For LLM calls.
        vault:            For persisting the new/updated tool.
        execution_id:     Audit linkage to the originating execution.

    Returns:
        :class:`RecalibrationResult` ready to surface on an operator
        approval card.
    """
    if diagnosis.recalibration_mode == "bump_version":
        result = _bump_version(
            tool=tool, diagnosis=diagnosis, failure_context=failure_context,
            config=config, vault=vault, execution_id=execution_id,
        )
        if result.success:
            return result
        # If the bump failed because of regression — fall back to fork.
        logger.info(
            "[Recalibrator] bump_version failed for %s — falling back to fork",
            tool.name,
        )
        result.forced_fallback = True
        # Forge a fork instead, keeping the original tool untouched.
        fork_result = _fork_new_tool(
            tool=tool, diagnosis=diagnosis, failure_context=failure_context,
            config=config, vault=vault, execution_id=execution_id,
        )
        fork_result.forced_fallback = True
        # Carry the bump's failure into the result so the operator can see why
        # we forked instead.
        fork_result.replay_status = result.replay_status
        fork_result.replay_error  = result.replay_error
        return fork_result

    if diagnosis.recalibration_mode == "fork_new_tool":
        return _fork_new_tool(
            tool=tool, diagnosis=diagnosis, failure_context=failure_context,
            config=config, vault=vault, execution_id=execution_id,
        )

    return RecalibrationResult(
        success=False, mode="aborted",
        original_tool_id=tool.id,
        rationale=diagnosis.rationale,
        error=f"unsupported recalibration mode: {diagnosis.recalibration_mode!r}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# bump_version path

def _bump_version(
    *,
    tool: "Tool",
    diagnosis: "InadequacyDiagnosis",
    failure_context: str,
    config: "Config",
    vault: "Vault",
    execution_id: str,
) -> RecalibrationResult:
    """Re-forge the tool in place, dry-run + replay validate."""
    from systemu.pipelines.tool_dry_run import (
        dry_run_tool, record_evolution, replay_against_history,
    )

    # Capture the old spec for v0.5.1-b diff before the in-place mutation.
    old_spec_snapshot = {
        "description":         tool.description,
        "parameters_schema":   dict(tool.parameters_schema or {}),
        "return_schema":       dict(tool.return_schema or {}),
        "implementation_notes": tool.implementation_notes,
        "dependencies":        list(tool.dependencies or []),
    }

    # Step 1: re-forge spec + code with the failure context.
    new_spec, new_code, forge_err = _reforge_tool(
        tool=tool, diagnosis=diagnosis,
        failure_context=failure_context, config=config,
    )
    if forge_err is not None:
        return RecalibrationResult(
            success=False, mode="bump_version",
            original_tool_id=tool.id, new_tool_id=tool.id,
            new_tool_name=tool.name,
            rationale=diagnosis.rationale,
            spec_diff_summary=diagnosis.spec_diff_summary,
            error=f"reforge failed: {forge_err}",
        )

    # Step 2: write the new code to the tool's existing implementation path.
    try:
        impl_path = Path(config.vault_dir) / "tools" / "implementations" / f"{tool.name}.py"
        impl_path.parent.mkdir(parents=True, exist_ok=True)
        impl_path.write_text(new_code, encoding="utf-8")
    except Exception as exc:
        return RecalibrationResult(
            success=False, mode="bump_version",
            original_tool_id=tool.id, new_tool_id=tool.id,
            new_tool_name=tool.name,
            rationale=diagnosis.rationale,
            spec_diff_summary=diagnosis.spec_diff_summary,
            error=f"could not write new code: {exc}",
        )

    # Update spec fields on the existing tool record.
    tool.parameters_schema = new_spec.get("parameters_schema") or tool.parameters_schema
    tool.return_schema = new_spec.get("return_schema") or tool.return_schema
    tool.implementation_notes = new_spec.get("implementation_notes") or tool.implementation_notes
    tool.description = new_spec.get("description") or tool.description
    # Keep enabled=False until operator approves the recalibration.
    tool.enabled = False

    # Step 3: dry-run with fresh test params.
    dr = dry_run_tool(tool, vault=vault, config=config, prior_failure=failure_context)
    tool.dry_run_status = dr.status
    tool.dry_run_evidence = dr.to_evidence()
    if not dr.success:
        vault.save_tool(tool)
        return RecalibrationResult(
            success=False, mode="bump_version",
            original_tool_id=tool.id, new_tool_id=tool.id,
            new_tool_name=tool.name,
            dry_run_status=dr.status, dry_run_error=dr.error or "",
            rationale=diagnosis.rationale,
            spec_diff_summary=diagnosis.spec_diff_summary,
            error="dry-run of new bump version failed",
        )

    # Step 4: replay against historical params to verify no regression.
    replay = replay_against_history(tool, vault=vault, config=config)
    if not replay.success:
        # Regression: bump rejected.  Caller falls back to fork.
        vault.save_tool(tool)
        return RecalibrationResult(
            success=False, mode="bump_version",
            original_tool_id=tool.id, new_tool_id=tool.id,
            new_tool_name=tool.name,
            dry_run_status=dr.status,
            replay_status="failed", replay_error=replay.error or "",
            rationale=diagnosis.rationale,
            spec_diff_summary=diagnosis.spec_diff_summary,
            error="backward-compat replay failed",
        )

    # Step 5: success — record evolution and persist.
    record_evolution(
        tool, mode="bump",
        reason=diagnosis.rationale[:300],
        diff_summary=diagnosis.spec_diff_summary[:300],
        vault=vault,
    )
    return RecalibrationResult(
        success=True, mode="bump_version",
        original_tool_id=tool.id, new_tool_id=tool.id,
        new_tool_name=tool.name,
        dry_run_status=dr.status,
        replay_status=("passed" if replay.replayed_count > 0 else "n/a"),
        rationale=diagnosis.rationale,
        spec_diff_summary=diagnosis.spec_diff_summary,
        spec_diff=compute_spec_diff(old_spec_snapshot, {
            "description":         tool.description,
            "parameters_schema":   tool.parameters_schema or {},
            "return_schema":       tool.return_schema or {},
            "implementation_notes": tool.implementation_notes,
            "dependencies":        tool.dependencies or [],
        }),
    )


# ─────────────────────────────────────────────────────────────────────────────
# fork_new_tool path

def _fork_new_tool(
    *,
    tool: "Tool",
    diagnosis: "InadequacyDiagnosis",
    failure_context: str,
    config: "Config",
    vault: "Vault",
    execution_id: str,
) -> RecalibrationResult:
    """Forge a brand-new tool record specialised for the failing shadow."""
    from systemu.core.models import Tool as ToolModel, ToolStatus, ToolType
    from systemu.core.utils import generate_id
    from systemu.pipelines.tool_dry_run import (
        dry_run_tool, record_evolution,
    )

    new_name = (
        (diagnosis.new_tool_name_suggestion or "").strip()
        or f"{tool.name}_v{(tool.version or 1) + 1}_specialised"
    )
    # Sanitise — only allow snake-case-ish names.
    new_name = "".join(c if c.isalnum() or c == "_" else "_" for c in new_name).strip("_")
    if not new_name:
        new_name = f"{tool.name}_forked"

    new_spec, new_code, forge_err = _reforge_tool(
        tool=tool, diagnosis=diagnosis,
        failure_context=failure_context, config=config,
        target_name=new_name,
    )
    if forge_err is not None:
        return RecalibrationResult(
            success=False, mode="fork_new_tool",
            original_tool_id=tool.id, new_tool_name=new_name,
            rationale=diagnosis.rationale,
            spec_diff_summary=diagnosis.spec_diff_summary,
            error=f"reforge failed: {forge_err}",
        )

    # Build a new tool record (new id).
    try:
        impl_path = Path(config.vault_dir) / "tools" / "implementations" / f"{new_name}.py"
        impl_path.parent.mkdir(parents=True, exist_ok=True)
        impl_path.write_text(new_code, encoding="utf-8")
    except Exception as exc:
        return RecalibrationResult(
            success=False, mode="fork_new_tool",
            original_tool_id=tool.id, new_tool_name=new_name,
            rationale=diagnosis.rationale,
            spec_diff_summary=diagnosis.spec_diff_summary,
            error=f"could not write fork code: {exc}",
        )

    new_tool = ToolModel(
        id=generate_id("tool"),
        name=new_name,
        description=(new_spec.get("description")
                      or f"Specialisation of {tool.name}: {diagnosis.spec_diff_summary[:200]}"),
        tool_type=tool.tool_type,
        parameters_schema=new_spec.get("parameters_schema") or tool.parameters_schema,
        return_schema=new_spec.get("return_schema") or tool.return_schema,
        implementation_notes=new_spec.get("implementation_notes") or tool.implementation_notes,
        dependencies=new_spec.get("dependencies") or tool.dependencies or [],
        implementation_path=str(impl_path.relative_to(Path(config.vault_dir).parent)),
        status=ToolStatus.FORGED,
        forged_by_systemu=True,
        enabled=False,
        version=1,
    )
    vault.save_tool(new_tool)

    # Dry-run the fork (no replay — nobody used this tool before).
    dr = dry_run_tool(new_tool, vault=vault, config=config, prior_failure=failure_context)
    new_tool.dry_run_status = dr.status
    new_tool.dry_run_evidence = dr.to_evidence()
    vault.save_tool(new_tool)

    if not dr.success:
        return RecalibrationResult(
            success=False, mode="fork_new_tool",
            original_tool_id=tool.id, new_tool_id=new_tool.id, new_tool_name=new_name,
            dry_run_status=dr.status, dry_run_error=dr.error or "",
            replay_status="n/a",
            rationale=diagnosis.rationale,
            spec_diff_summary=diagnosis.spec_diff_summary,
            error="dry-run of forked tool failed",
        )

    # Audit on the new tool: forked from origin.
    record_evolution(
        new_tool, mode="fork",
        reason=f"forked from {tool.name} (id={tool.id}): {diagnosis.rationale[:200]}",
        diff_summary=diagnosis.spec_diff_summary[:300],
        vault=vault,
    )

    # Build the v0.5.1-b spec diff between original tool and new fork.
    old_spec_snapshot = {
        "description":         tool.description,
        "parameters_schema":   dict(tool.parameters_schema or {}),
        "return_schema":       dict(tool.return_schema or {}),
        "implementation_notes": tool.implementation_notes,
        "dependencies":        list(tool.dependencies or []),
    }
    new_spec_snapshot = {
        "description":         new_tool.description,
        "parameters_schema":   new_tool.parameters_schema or {},
        "return_schema":       new_tool.return_schema or {},
        "implementation_notes": new_tool.implementation_notes,
        "dependencies":        new_tool.dependencies or [],
    }

    return RecalibrationResult(
        success=True, mode="fork_new_tool",
        original_tool_id=tool.id,
        new_tool_id=new_tool.id, new_tool_name=new_name,
        dry_run_status=dr.status,
        replay_status="n/a",
        rationale=diagnosis.rationale,
        spec_diff_summary=diagnosis.spec_diff_summary,
        spec_diff=compute_spec_diff(old_spec_snapshot, new_spec_snapshot),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reforge — shared between bump + fork

def _reforge_tool(
    *,
    tool: "Tool",
    diagnosis: "InadequacyDiagnosis",
    failure_context: str,
    config: "Config",
    target_name: Optional[str] = None,
) -> tuple[Dict[str, Any], str, Optional[str]]:
    """Re-run the v0.3.4 forge spec + code path with the failure context.

    Returns ``(new_spec, new_code, error_message_or_None)``.  The caller
    decides whether to write the code to disk (bump) or as a new file
    (fork — uses ``target_name``).
    """
    try:
        from systemu.core.llm_router import llm_call_json
        from systemu.core.utils import load_prompt
        # Re-spec.
        spec_payload = {
            "tool_name":           target_name or tool.name,
            "scroll_narrative":    f"Recalibrating tool '{tool.name}' due to: {failure_context}",
            "preferred_packages":  list(tool.dependencies or []),
            "prior_dry_run_failure": (
                f"Diagnosis: {diagnosis.rationale}\n"
                f"Spec diff intent: {diagnosis.spec_diff_summary}"
            ),
        }
        spec_result = llm_call_json(
            tier=2,
            system=load_prompt("forge_tool_spec.md"),
            user=json.dumps(spec_payload, ensure_ascii=False),
            config=config,
            temperature=0.2,
            max_tokens=2048,
        )
        if not isinstance(spec_result, dict):
            return ({}, "", "spec LLM returned non-object")

        # Re-code.
        code_payload = {
            "tool_spec":           spec_result,
            "implementation_notes": spec_result.get("implementation_notes", ""),
        }
        code_result = llm_call_json(
            tier=2,
            system=load_prompt("forge_tool_code.md"),
            user=json.dumps(code_payload, ensure_ascii=False),
            config=config,
            temperature=0.2,
            max_tokens=4096,
        )
        if not isinstance(code_result, dict):
            return (spec_result, "", "code LLM returned non-object")

        code = str(code_result.get("implementation") or "").strip()
        if not code:
            return (spec_result, "", "code LLM returned empty implementation")
        return (spec_result, code, None)
    except Exception as exc:
        logger.exception("[Recalibrator] reforge crashed")
        return ({}, "", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Operator approval card

def publish_recalibration_card(
    *,
    result: RecalibrationResult,
    shadow_id: str,
    execution_id: str,
    scroll_id: Optional[str] = None,
) -> None:
    """Surface the recalibration outcome to the operator chat feed.

    Reuses the v0.3.6 supervisor-flash bus pattern: category="approval",
    redirect_to="/tools", dedup_key keyed on (tool × execution) so the
    same recalibration doesn't flood the feed if the supervisor re-fires.
    """
    try:
        from datetime import datetime, timezone
        from systemu.interface.event_bus import EventBus
        bus = EventBus.get()
        glyph = "✅" if result.success else "⚠️"
        title = (
            f"🔁 Tool recalibration {result.mode}"
            + (" — fallback to fork" if result.forced_fallback else "")
        )
        bus.publish({
            "ts":       datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "level":    "WARNING" if not result.success else "INFO",
            "category": "approval",
            "message":  f"{glyph} {title}",
            "context": {
                "approval_message": _compose_approval_message(result),
                "options":          [],
                "redirect_to":      "/tools",
                "dedup_key":        f"tool-recalibrate:{result.original_tool_id}:{execution_id}",
                "execution_id":     execution_id,
                "shadow_id":        shadow_id,
                "scroll_id":        scroll_id,
                "recalibration":    result.to_card_context(),
                # actions field expected by the workflow detail panel.
                "actions":          [
                    "enable_recalibrated_tool",
                    "override_to_bump",
                    "override_to_fork",
                    "reject",
                ],
            },
        })
    except Exception:
        logger.debug("[Recalibrator] approval card publish skipped", exc_info=True)


def _compose_approval_message(result: RecalibrationResult) -> str:
    lines = [
        f"**Mode:** {result.mode}"
        + (" (fallback — bump replay failed)" if result.forced_fallback else ""),
        f"**Reason:** {result.rationale}",
        f"**Change:** {result.spec_diff_summary or '(see audit)'}",
        f"**Dry-run:** {result.dry_run_status}"
        + (f" — {result.dry_run_error}" if result.dry_run_error else ""),
    ]
    if result.replay_status and result.replay_status != "n/a":
        lines.append(
            f"**Backward-compat replay:** {result.replay_status}"
            + (f" — {result.replay_error}" if result.replay_error else "")
        )
    if result.new_tool_name:
        lines.append(f"**New tool:** {result.new_tool_name}")
    if not result.success:
        lines.append(f"\n**Error:** {result.error or '(see audit)'}")
    lines.append("\nReview on /tools and choose: enable + auto-map to shadow, override mode, or reject.")
    return "\n".join(lines)
