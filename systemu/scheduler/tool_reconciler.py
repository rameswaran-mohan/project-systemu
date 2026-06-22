"""Tool lifecycle reconciler (v0.7.4 Pattern 2 from the 2026-05-26 audit).

Runs every 30 seconds inside the daemon. For each Tool whose status is
FORGED (or PROPOSED with an implementation_path) and whose dry-run hasn't
been recorded yet, dispatch the existing dry_run_tool pipeline. On pass,
advance status to DEPLOYED. On fail, leave at FORGED with
dry_run_status='failed' and publish a quality-event for operator visibility.

This closes the v0.7.3 UAT Bug #22 root cause (forged tools rotted at
FORGED forever) without changing the dry-run code itself — it just
makes the trigger fire on the right schedule and on the right scope.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from systemu.vault.vault import Vault
    from sharing_on.config import Config


def reconcile_once(vault: "Vault", config: "Config") -> int:
    """One reconciliation pass. Returns count of tools processed."""
    from systemu.scheduler.jobs import _find_pending_dry_run_via_index
    from systemu.pipelines.tool_dry_run import dry_run_tool
    from systemu.core.models import ToolStatus
    from systemu.interface.notifications import log_event

    try:
        headers = vault.load_index("tools") or []
    except Exception:
        logger.exception("[ToolReconciler] failed to load tools index")
        return 0

    pending = _find_pending_dry_run_via_index(headers)
    if not pending:
        return 0

    logger.info("[ToolReconciler] %d tool(s) pending dry-run", len(pending))
    processed = 0
    for header in pending:
        tool_id = header.get("id")
        try:
            tool = vault.get_tool(tool_id)
        except Exception:
            logger.warning("[ToolReconciler] could not load tool %s", tool_id, exc_info=True)
            continue

        if not getattr(tool, "implementation_path", None):
            # Still at PROPOSED with no code; skip until forge completes
            continue

        try:
            result = dry_run_tool(tool, vault=vault, config=config)
        except Exception:
            logger.exception("[ToolReconciler] dry_run_tool crashed for %s", tool_id)
            continue

        tool.dry_run_status = result.status
        if result.status == "passed":
            tool.status = ToolStatus.DEPLOYED
            vault.save_tool(tool)
            logger.info(
                "[ToolReconciler] tool '%s' (%s) -> DEPLOYED (dry-run passed in %dms)",
                tool.name, tool_id, getattr(result, "elapsed_ms", 0),
            )
        elif result.status == "skipped":
            # No state change; just record the skip reason
            vault.save_tool(tool)
            logger.info(
                "[ToolReconciler] tool '%s' (%s) dry-run SKIPPED: %s",
                tool.name, tool_id, getattr(result, "skip_reason", "unknown"),
            )
        else:  # "failed"
            vault.save_tool(tool)
            log_event(
                "WARNING", "tool",
                f"Tool '{tool.name}' failed dry-run validation: {(result.error or '')[:200]}",
                {"tool_id": tool_id, "tool_name": tool.name, "error": result.error},
            )
            logger.warning(
                "[ToolReconciler] tool '%s' (%s) dry-run FAILED — left at FORGED",
                tool.name, tool_id,
            )
        processed += 1

    return processed
