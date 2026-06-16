"""Dispatcher handler for ``command:<sig>`` operator decisions (v0.9.32, D-3/D.3).

The command gate (ToolSandbox chokepoint) posts a three-way decision:
Deny / Approve once / Always allow. This handler runs when the operator
resolves it:

  * "Always allow" -> persist the EXACT command signature to the
    CommandApprovalStore so future identical runs skip the gate.
  * "Approve once" -> no persistence; the waiting lane re-reads the choice
    via get_resolved_choice and runs this one time.
  * "Deny"         -> no persistence; the waiting lane sees Deny (fail-closed).

Registered into the decision_dispatcher bootstrap (decision_dispatcher.py)
so dispatch() routes ``command:*`` here even when nothing else imported us
(the bootstrap-import trap: register() alone is not enough — the module must
be force-imported during _ensure_handlers_registered or dispatch silently
no-ops for this namespace).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _handle_resolved_command(decision, choice, config, vault) -> None:
    norm = (choice or "").strip().lower()
    if norm != "always allow":
        logger.info("[CommandGate] resolved %r — no persistence (one-shot/deny)",
                    choice)
        return

    ctx = getattr(decision, "context", None) or {}
    command = ctx.get("command", "") or ""
    cwd = ctx.get("cwd", "") or ""

    # Prefer recomputing the signature from the carried command+cwd so the
    # stored key is canonical; fall back to the dedup_key suffix if the
    # context didn't round-trip the raw command.
    from systemu.runtime.command_approvals import (
        command_signature, get_default_store, init_default_store)
    from pathlib import Path as _Path

    if command:
        sig = command_signature(command, cwd=cwd)
    else:
        sig = (getattr(decision, "dedup_key", "") or "").partition(":")[2]

    store = get_default_store() or init_default_store(_Path("data"))
    if sig:
        store.approve(sig, command=command, cwd=cwd)  # approved_by defaults to 'operator'
        logger.info("[CommandGate] persisted Always-allow for %s", sig)


from systemu.approval.decision_dispatcher import register as _register_dispatch
_register_dispatch("command", _handle_resolved_command)
