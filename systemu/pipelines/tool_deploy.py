"""Synchronous deploy-forged-tool pipeline (v0.9.7 Phase 2.2).

Provides a single function, ``deploy_forged_tool``, that advances a
FORGED tool to DEPLOYED+enabled=True in one synchronous call — suitable
for the auto-grant path where operator approval is not required (e.g.
REQUEST_HARNESS GRANT in headless / dev mode).

Design:
* Reuses the existing ``dry_run_tool`` function from
  ``systemu.pipelines.tool_dry_run`` verbatim — no duplication of
  dry-run mechanics.
* On pass  → sets status=DEPLOYED, enabled=True, saves via vault.
* On fail  → leaves tool at FORGED / enabled=False, returns reason.
* Already deployed → no-op, returns {"deployed": True, "already": True}.
* Never raises into the caller — all errors surface as
  {"deployed": False, "reason": <str>}.

A tool is "callable" in a normal (non-dry-run) run when ALL of the
following hold:
  1. tool.status in {DEPLOYED, TESTED, UPGRADED}  (the _load_tools gate)
  2. tool.enabled == True                          (Gate 3 / _tool_gate_check)
Both are set atomically by this function when the dry-run passes.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Any

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

# Statuses that already count as "callable" — no work needed.
_CALLABLE_STATUSES = {"deployed", "tested", "upgraded"}


def deploy_forged_tool(
    tool_id: str,
    vault: "Vault",
    config: "Config",
) -> Dict[str, Any]:
    """Synchronously validate and deploy a FORGED tool so it is callable
    in the same run.

    Args:
        tool_id: Vault ID of the tool to deploy.
        vault:   Vault instance for loading and saving the tool.
        config:  Config instance forwarded to the dry-run pipeline.

    Returns:
        A plain dict — callers can inspect without importing this module:

        * ``{"deployed": True, "already": True}``  — tool was already in a
          callable state (DEPLOYED/TESTED/UPGRADED + enabled); no-op.
        * ``{"deployed": True}``  — dry-run passed; tool promoted to
          DEPLOYED + enabled=True and persisted.
        * ``{"deployed": False, "reason": "<str>"}``  — dry-run failed or
          was skipped; tool left at FORGED / enabled=False.  Reason
          describes what went wrong.
        * ``{"deployed": False, "reason": "tool_not_found"}``  — unknown
          tool_id; safe to handle without raising.

    Never raises.
    """
    # ── Load the tool ──────────────────────────────────────────────────────
    try:
        tool = vault.get_tool(tool_id)
    except (KeyError, Exception) as exc:
        logger.warning(
            "[DeployForgedTool] tool %s not found: %s", tool_id, exc
        )
        return {"deployed": False, "reason": "tool_not_found"}

    # ── Already in a callable state? ───────────────────────────────────────
    # Both conditions (status and enabled) must hold for the tool to be truly
    # callable.  If it's already deployed+enabled, no work is needed.
    status_val = (getattr(tool, "status", None) or "").value \
        if hasattr(getattr(tool, "status", None), "value") \
        else str(getattr(tool, "status", "") or "").lower()

    already_callable = (
        status_val in _CALLABLE_STATUSES
        and bool(getattr(tool, "enabled", False))
    )
    if already_callable:
        logger.debug(
            "[DeployForgedTool] tool '%s' (%s) already callable (status=%s, enabled=True)",
            getattr(tool, "name", tool_id), tool_id, status_val,
        )
        return {"deployed": True, "already": True}

    # ── Dry-run gate ───────────────────────────────────────────────────────
    # Import lazily so tests can monkeypatch
    # systemu.pipelines.tool_dry_run.dry_run_tool without importing this
    # module first.
    try:
        from systemu.pipelines import tool_dry_run as _dr_module
        dry_run_fn = _dr_module.dry_run_tool
    except Exception as exc:
        logger.error(
            "[DeployForgedTool] could not import dry_run_tool: %s", exc
        )
        return {"deployed": False, "reason": f"import_error: {exc}"}

    try:
        result = dry_run_fn(tool, vault=vault, config=config)
    except Exception as exc:
        logger.exception(
            "[DeployForgedTool] dry_run_tool raised unexpectedly for %s", tool_id
        )
        return {"deployed": False, "reason": f"dry_run_exception: {exc}"}

    # ── Promote or leave ───────────────────────────────────────────────────
    if result.success:
        try:
            from systemu.core.models import ToolStatus
            tool.status = ToolStatus.DEPLOYED
            tool.enabled = True
            tool.dry_run_status = result.status
            tool.dry_run_evidence = result.to_evidence()
            vault.save_tool(tool)
            logger.info(
                "[DeployForgedTool] tool '%s' (%s) promoted to DEPLOYED+enabled "
                "(dry-run passed in %dms)",
                getattr(tool, "name", tool_id),
                tool_id,
                getattr(result, "elapsed_ms", 0),
            )
            return {"deployed": True}
        except Exception as exc:
            logger.exception(
                "[DeployForgedTool] vault.save_tool failed for %s", tool_id
            )
            return {"deployed": False, "reason": f"save_error: {exc}"}

    # Dry-run failed or was skipped — leave the tool as-is.
    reason = (
        getattr(result, "error", None)
        or getattr(result, "skip_reason", None)
        or f"dry_run_{result.status}"
    )
    logger.warning(
        "[DeployForgedTool] tool '%s' (%s) dry-run %s — not deployed. reason=%r",
        getattr(tool, "name", tool_id), tool_id, result.status, reason,
    )
    # Persist the dry-run evidence even on failure so operators can inspect it.
    try:
        tool.dry_run_status = result.status
        tool.dry_run_evidence = result.to_evidence()
        vault.save_tool(tool)
    except Exception:
        logger.debug(
            "[DeployForgedTool] could not persist failed dry-run evidence for %s",
            tool_id, exc_info=True,
        )
    return {"deployed": False, "reason": str(reason)[:500]}
