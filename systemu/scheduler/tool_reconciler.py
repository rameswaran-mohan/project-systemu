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
        _fail_unsatisfiable_blocked_activities(vault, config)
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
        else:  # dry-run failed
            err = result.error or ""
            healed = False
            # v0.9.48 Phase 7 — bounded, Governor-gated self-heal. Ask the Governor
            # whether this failure is a re-forgeable CODE bug (under the per-tool
            # cap); on a grant, re-forge ONCE — feeding the error back into the code
            # prompt as an authoritative course-correction — and re-dry-run IN THIS
            # TICK. `dry_run_status='failed'` is persisted only in the not-healed
            # branch below, which runs before _fail_unsatisfiable_blocked_activities
            # (end of this pass), so the reaper still fires exactly once, after the
            # single attempt is spent. A dep/permission failure is NOT granted and
            # falls straight through to the unchanged terminal path.
            try:
                from systemu.runtime.governor import Governor
                decision = Governor(config).review_reforge(tool, err)
            except Exception:
                logger.debug("[ToolReconciler] review_reforge errored for %s", tool_id, exc_info=True)
                decision = None
            if decision is not None and getattr(decision, "granted", False):
                tool.forge_reattempts = (getattr(tool, "forge_reattempts", 0) or 0) + 1
                try:
                    from systemu.pipelines.tool_forge import reforge_failed_tool_code
                    forged = reforge_failed_tool_code(
                        tool, config, vault, prior_failure=decision.course_correction)
                    if forged is not None:
                        result = dry_run_tool(tool, vault=vault, config=config)
                        tool.dry_run_status = result.status
                        if result.status in ("passed", "skipped"):
                            if result.status == "passed":
                                tool.status = ToolStatus.DEPLOYED
                            vault.save_tool(tool)
                            log_event(
                                "SUCCESS", "tool",
                                f"Tool '{tool.name}' self-healed on dry-run re-forge ({result.status})",
                                {"tool_id": tool_id, "tool_name": tool.name},
                            )
                            logger.info(
                                "[ToolReconciler] tool '%s' (%s) SELF-HEALED -> %s",
                                tool.name, tool_id, result.status,
                            )
                            healed = True
                except Exception:
                    logger.exception("[ToolReconciler] re-forge self-heal crashed for %s", tool_id)
            if not healed:
                tool.dry_run_status = result.status   # terminal 'failed'
                vault.save_tool(tool)
                # v0.9.48 Phase 3: a fresh `failed` dry-run must auto-disable a tool
                # that was already DEPLOYED+enabled, so it can't stay callable.
                try:
                    from systemu.pipelines.tool_service import disable_if_dry_run_failed
                    disable_if_dry_run_failed(tool_id, vault)
                except Exception:
                    logger.debug(
                        "[ToolReconciler] disable_if_dry_run_failed failed for %s",
                        tool_id, exc_info=True,
                    )
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
    _fail_unsatisfiable_blocked_activities(vault, config)
    return processed


def _fail_unsatisfiable_blocked_activities(vault: "Vault", config: "Config") -> None:
    """v0.9.48 Phase 4.2: the daemon-side safety net for a parked activity whose
    awaited REQUIRED tool can never deploy — even if the operator never resolves
    the tools_blocked gate.

    Walks every PARTIAL activity; if its ``required_tool_ids`` include a tool with
    ``dry_run_status == "failed"`` (STRICTLY — never ``!= "passed"``: a `skipped`/
    operator-verify or `not_run` tool is NOT permanent and must NOT be reaped),
    finalize the activity FAILED with the dry-run error surfaced, and flip its
    parked ``waiting_on_tools`` chat entry. Idempotent — an already-terminal
    activity is skipped (the index filter keeps only PARTIAL), so this is safe on
    every 30s tick. Best-effort throughout; never raises into the reconcile loop.
    """
    from systemu.core.models import ActivityStatus
    from systemu.runtime.activity_completion import mark_activity_failed
    from systemu.interface.notifications import log_event

    try:
        partials = vault.list_activities(status=ActivityStatus.PARTIAL)
    except Exception:
        logger.debug("[ToolReconciler] reap: could not list PARTIAL activities", exc_info=True)
        return

    for header in partials:
        act_id = header.get("id")
        if not act_id:
            continue
        permanently_failed = []
        for tid in (header.get("required_tool_ids") or []):
            try:
                tool = vault.get_tool(tid)
            except Exception:
                continue
            if (getattr(tool, "dry_run_status", "") or "") == "failed":
                err = (getattr(tool, "dry_run_evidence", None) or {}).get("error") or ""
                permanently_failed.append((getattr(tool, "name", tid) or tid, err))
        if not permanently_failed:
            continue

        names = ", ".join(n for n, _ in permanently_failed)
        errs = " | ".join(f"{n}: {e}" for n, e in permanently_failed if e)
        summary = (
            f"Required tool(s) can never deploy (dry-run failed): {names}. "
            + (f"Dry-run error: {errs}. " if errs else "")
            + "Task finalized FAILED — re-forge a conforming tool to retry.")
        if not mark_activity_failed(vault, act_id, summary=summary):
            continue
        # Best-effort: flip the parked waiting_on_tools chat entry to failed.
        try:
            for entry in vault.load_chat_history(limit=50):
                if (entry.get("activity_id") == act_id
                        and entry.get("status") == "waiting_on_tools"):
                    vault.update_chat_history_entry(
                        entry.get("ts"), {"status": "failed", "error": summary})
                    break
        except Exception:
            logger.debug(
                "[ToolReconciler] reap: could not flip chat entry for %s",
                act_id, exc_info=True)
        try:
            log_event(
                "ERROR", "activity",
                f"Activity {act_id} finalized FAILED — un-deployable required tool(s): {names}",
                {"activity_id": act_id, "tool_names": names, "dry_run_errors": errs})
        except Exception:
            logger.debug("[ToolReconciler] reap: log_event failed for %s", act_id, exc_info=True)
        logger.info(
            "[ToolReconciler] reaped unsatisfiable activity %s — un-deployable tool(s): %s",
            act_id, names)


def _is_operator_verify_skip(tool) -> bool:
    """v0.9.48 Phase 3: True for a Phase 1 operator-verify skip — a tool whose
    dry-run was skipped because the harness couldn't synthesize a representative
    input (e.g. a real .docx) and the operator owns correctness. Such a tool is
    enable-able, so the deferred-enable path must complete it like a `passed`."""
    if (getattr(tool, "dry_run_status", "") or "") != "skipped":
        return False
    evidence = getattr(tool, "dry_run_evidence", None) or {}
    return bool(isinstance(evidence, dict) and evidence.get("operator_verify"))


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
            # Only enable once the dry-run has actually passed (Gate-3.5 intent)
            # OR it is a Phase 1 operator-verify skip (enable-able; operator owns
            # correctness). enable_tool itself re-gates on passed/skipped.
            if getattr(tool, "dry_run_status", "") != "passed" and not _is_operator_verify_skip(tool):
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
