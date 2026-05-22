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


def heal_activities_for_tool(tool_id: str, config: "Config", vault: "Vault") -> None:
    """Public alias — call this after enable_tool() to unblock PARTIAL activities.

    Blocking: contains LLM calls (shadow_decision). Run in a background thread
    from async contexts (e.g. NiceGUI event loop).
    """
    _heal_partial_activities(tool_id, config, vault)


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
