"""tool_service.py — Canonical operations on Tool records.

All code paths that enable or disable a tool must go through this module so
that the FORGED → DEPLOYED status transition and the PARTIAL-activity heal
chain are never skipped.  Direct vault.save_tool() calls that flip .enabled
bypass both and leave the vault in an inconsistent state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from systemu.core.models import ActivityStatus, ToolStatus

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# v0.9.48 Phase 3: the single authoritative enable taxonomy. A tool may be
# enabled only when its dry-run either passed or was skipped (the latter
# covers both the safety-skip and the Phase 1 operator_verify skip — the
# operator owns correctness). Only a `failed` (known-broken) status is refused
# here, at the mechanism FLOOR, so NO caller (recalibration, tools_blocked, CLI,
# the deferred-enable reconciler) can deploy a tool whose dry-run FAILED.
# `not_run` stays enable-able at this floor: the reviewed-approve / dep-install
# flows legitimately enable an operator-vetted tool that hasn't been dry-run yet.
# Stricter "must have PASSED" validation is enforced by the paths that need it
# (e.g. the readiness-gate verb). A tool that later records a `failed` dry-run is
# auto-disabled (disable_if_dry_run_failed), so this floor stays safe.
ENABLE_BLOCKED_DRY_RUN_STATUSES = frozenset({"failed"})


def can_enable(tool) -> bool:
    """False only when the tool's dry-run definitively FAILED — the floor that
    keeps a known-broken tool from ever reaching DEPLOYED. passed / skipped /
    not_run are all enable-able here."""
    return (getattr(tool, "dry_run_status", "not_run") or "not_run") not in ENABLE_BLOCKED_DRY_RUN_STATUSES


def enable_tool(tool_id: str, vault: "Vault") -> bool:
    """Enable a FORGED tool and advance it to DEPLOYED.

    This is the fast, synchronous part — it only touches the vault record.
    Call heal_activities_for_tool() afterwards (in a background thread from the
    UI, or inline from a script) to trigger shadow assignment on any PARTIAL
    activities that were waiting for this tool.

    Safe to call from scripts, migrations, CLI, and the UI toggle handler.
    Returns True if the tool was enabled, False if already enabled or not found.
    """
    from systemu.interface.notifications import log_event

    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        logger.warning("[ToolService] enable_tool: tool %s not found", tool_id)
        return False

    if tool.enabled:
        return False

    # v0.9.48 Phase 3: the authoritative enable gate. Refuse to deploy a tool
    # whose dry-run definitively FAILED. This closes every enable path at the
    # mechanism so a `dry_run_status="failed"` tool can never reach DEPLOYED,
    # regardless of which caller (recalibration launder, tools_blocked, CLI,
    # deferred-enable) attempted it.
    if not can_enable(tool):
        log_event(
            "WARNING", "tool",
            f"Tool '{tool.name}' enable refused: "
            f"dry_run_status={getattr(tool, 'dry_run_status', 'not_run')!r} "
            f"(a failed dry-run is never enable-able)",
            {"tool_id": tool.id},
        )
        logger.warning(
            "[ToolService] enable_tool refused %s: dry_run_status=%s",
            tool.id, getattr(tool, "dry_run_status", "not_run"),
        )
        return False

    tool.enabled = True
    if tool.status == ToolStatus.FORGED:
        tool.status = ToolStatus.DEPLOYED
    vault.save_tool(tool)

    log_event(
        "SUCCESS", "tool",
        f"Tool '{tool.name}' enabled → DEPLOYED",
        {"tool_id": tool.id},
    )
    logger.info("[ToolService] Tool '%s' enabled → DEPLOYED", tool.name)
    return True


def disable_tool(tool_id: str, vault: "Vault") -> bool:
    """Disable a DEPLOYED tool, revert it to FORGED.

    Returns True if the tool was disabled, False if already disabled or not found.
    """
    from systemu.interface.notifications import log_event

    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        logger.warning("[ToolService] disable_tool: tool %s not found", tool_id)
        return False

    if not tool.enabled:
        return False

    tool.enabled = False
    if tool.status == ToolStatus.DEPLOYED:
        tool.status = ToolStatus.FORGED
    vault.save_tool(tool)

    log_event(
        "INFO", "tool",
        f"Tool '{tool.name}' disabled → FORGED",
        {"tool_id": tool.id},
    )
    logger.info("[ToolService] Tool '%s' disabled → FORGED", tool.name)
    return True


def disable_if_dry_run_failed(tool_id: str, vault: "Vault") -> bool:
    """v0.9.48 Phase 3: auto-disable a DEPLOYED+enabled tool whose FRESH dry-run
    just recorded ``dry_run_status="failed"``.

    Wired right after the dry-run persist in the scheduler (jobs.dry_run_one_tool
    and tool_reconciler.reconcile_once) so a tool that was deployed earlier but
    now fails validation can never stay callable. Only fires on a fresh dry-run
    `failed` — runtime call failures are the circuit-breaker / recalibration
    domain, not this one.

    Returns True iff the tool was disabled.
    """
    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        return False
    if getattr(tool, "dry_run_status", None) == "failed" and getattr(tool, "enabled", False):
        return disable_tool(tool_id, vault)
    return False


def heal_activities_for_tool(tool_id: str, config: "Config", vault: "Vault") -> None:
    """Public alias — call this after enable_tool() to unblock PARTIAL activities
    AND auto-advance any VALIDATOR_BLOCKED scrolls that this tool unblocks.

    Blocking: contains LLM calls (shadow_decision + validator).  Run in a
    background thread from async contexts (e.g. NiceGUI event loop).
    """
    _heal_partial_activities(tool_id, config, vault)

    # v0.8.5: also re-validate any VALIDATOR_BLOCKED scrolls — this tool
    # transitioning to DEPLOYED may have unblocked them.  Wrapped in
    # try/except so a validator failure never blocks tool deploy itself.
    try:
        from systemu.pipelines.scroll_refiner import revalidate_blocked_scrolls_for_tool
        revalidate_blocked_scrolls_for_tool(tool_id, config=config, vault=vault)
    except Exception:
        logger.exception(
            "[ToolService] re-validation hook raised for tool %s — non-fatal",
            tool_id,
        )


def _heal_partial_activities(tool_id: str, config: "Config", vault: "Vault") -> None:
    """Transition PARTIAL activities to UNASSIGNED when all their tools are deployed,
    then trigger shadow assignment for each healed activity.
    """
    from systemu.pipelines.shadow_decision import decide_shadow

    for a_header in vault.list_activities():
        if a_header.get("status") != ActivityStatus.PARTIAL.value:
            continue
        if tool_id not in (a_header.get("required_tool_ids") or []):
            continue

        try:
            activity = vault.get_activity(a_header["id"])
        except KeyError:
            continue

        all_ready = all(
            _tool_is_enabled(tid, vault)
            for tid in activity.required_tool_ids
        )
        if not all_ready:
            continue

        activity.status = ActivityStatus.UNASSIGNED
        activity.missing_tools = []
        vault.save_activity(activity)
        logger.info(
            "[ToolService] Activity '%s' healed: PARTIAL → UNASSIGNED — chaining to shadow decision",
            activity.name,
        )

        # v0.8.13 Fix 6b: live resume — if a parked (waiting_on_tools) chat entry
        # references this activity, flip it to running now (not only at startup
        # sweep). Lazy import avoids a circular import between tool_service and jobs.
        try:
            from systemu.scheduler.jobs import _resume_waiting_chat_entry
            _resume_waiting_chat_entry(vault, activity.id)
        except Exception:
            logger.debug("[ToolService] resume hook failed for %s", activity.id, exc_info=True)

        try:
            decide_shadow(activity, config, vault)
        except Exception as exc:
            logger.warning(
                "[ToolService] Shadow decision after heal failed for '%s': %s",
                activity.name, exc,
            )


def _tool_is_enabled(tool_id: str, vault: "Vault") -> bool:
    try:
        return vault.get_tool(tool_id).enabled
    except KeyError:
        return False
