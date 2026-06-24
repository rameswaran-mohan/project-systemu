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
        _complete_deferred_enables(vault, config)
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

    _complete_deferred_enables(vault, config)
    return processed


def _complete_deferred_enables(vault: "Vault", config: "Config") -> None:
    """v0.9.44: close the "Enable & run" / dry-run RACE — and recover runs already
    stuck by it.

    The Inbox "Enable & run" gate enables each blocking tool, but Gate-3.5 holds a
    tool that has NOT passed its dry-run yet. If the operator approves the gate
    BEFORE the reconciler finishes the dry-run (very common — the gate appears
    right after the forge gate), the enable is held and never retried: the tool
    ends up DEPLOYED-but-disabled and the parked task hangs forever, even though
    the heal sweep was wired into the resolver (it fired while the tool was still
    disabled, so it was a no-op).

    Every reconcile pass, for each RESOLVED "Enable & run" tools_blocked gate,
    enable any of its tools that are now dry-run-passed-but-disabled and fire the
    heal sweep so the parked activity re-dispatches. Idempotent: an already-enabled
    tool is skipped, so this is safe to run on every 30s tick.
    """
    try:
        decisions = vault.load_index("decisions") or []
    except Exception:
        logger.exception("[ToolReconciler] deferred-enable: could not load decisions")
        return

    from systemu.pipelines.tool_service import enable_tool, heal_activities_for_tool

    for header in decisions:
        if header.get("status") != "resolved":
            continue
        if not str(header.get("dedup_key") or "").startswith("tools_blocked:"):
            continue
        try:
            dec = vault.get_decision(header["id"])
        except Exception:
            continue
        if (getattr(dec, "choice", "") or "").strip().lower() != "enable & run":
            continue
        for tid in ((getattr(dec, "context", None) or {}).get("tool_ids") or []):
            try:
                tool = vault.get_tool(tid)
            except Exception:
                continue
            if getattr(tool, "enabled", False):
                continue
            # Only enable once the dry-run has actually passed (Gate-3.5 intent).
            if getattr(tool, "dry_run_status", "") != "passed":
                continue
            if enable_tool(tid, vault):
                logger.info(
                    "[ToolReconciler] completed deferred 'Enable & run' for '%s' "
                    "(%s) — operator approved before the dry-run finished; healing "
                    "parked tasks", getattr(tool, "name", tid), tid)
                # heal makes LLM calls (decide_shadow) — run off the reconciler tick.
                import threading
                threading.Thread(
                    target=heal_activities_for_tool,
                    args=(tid, config, vault),
                    daemon=True,
                ).start()
