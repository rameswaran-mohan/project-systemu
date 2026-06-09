"""Pure verb functions returning CommandResult.

No Click / NiceGUI imports at module level so both surfaces (and CI) can
import these directly. Each verb is the *canonical writer* for its domain
(see the Phase 2 mutation-ownership record).
"""
from __future__ import annotations

from systemu.interface.command.result import CommandResult, CommandStatus


def tools_enable(tool_id: str, *, vault) -> CommandResult:
    """Gate-3 enable for a tool — the ONE gated policy for the tool-enable domain.

    Consolidation (P2-T11): this verb is the single *policy* and DELEGATES the
    actual mutation to the single *mechanism*, ``tool_service.enable_tool``.
    The verb's value-add is the Gate-3.5 rule (models.py:291): a tool may only
    be enabled once ``dry_run_status == 'passed'``. The mechanism stays the one
    place that flips ``enabled``, advances FORGED→DEPLOYED, and logs the event —
    so the verb now gets the status-advance + log for free, and every surface
    (CLI, dashboard toggle, recovery panel) routes through this one verb.

    Layering:
      1. not found        → ERROR
      2. already enabled  → NOOP (mechanism not called)
      3. dry-run not pass → ERROR (the gate; mechanism not called)
      4. else             → delegate to tool_service.enable_tool (enable +
                            advance + log)
    """
    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        return CommandResult(status=CommandStatus.ERROR,
                             summary=f"Tool {tool_id!r} not found in vault.")

    if getattr(tool, "enabled", False):
        return CommandResult(status=CommandStatus.NOOP,
                             summary=f"Tool {tool.name} already enabled.",
                             data={"tool_id": tool_id})

    if getattr(tool, "dry_run_status", "not_run") != "passed":
        return CommandResult(
            status=CommandStatus.ERROR,
            summary=(f"Cannot enable {tool.name}: dry_run_status="
                     f"{getattr(tool, 'dry_run_status', 'not_run')!r} (must be 'passed')."),
            data={"tool_id": tool_id},
        )

    # Delegate the write to the one mechanism (lazy import keeps verbs.py
    # import-pure — no pipeline imports at module load).
    from systemu.pipelines import tool_service

    enabled = tool_service.enable_tool(tool_id, vault)
    if not enabled:
        # The mechanism declined (a concurrent enable, or the tool vanished
        # between the gate read and the write). Surface it rather than claiming
        # success — the gate already proved it was disabled + dry-run-passed.
        return CommandResult(
            status=CommandStatus.ERROR,
            summary=f"Could not enable {tool.name} (mechanism declined).",
            data={"tool_id": tool_id},
        )

    return CommandResult(
        status=CommandStatus.OK,
        summary=f"Enabled tool {tool.name}.",
        data={"tool_id": tool_id, "status": getattr(
            getattr(tool, "status", None), "value", getattr(tool, "status", None))},
    )


def tools_show(tool_id: str, *, vault) -> CommandResult:
    """Read-only inspection of a single tool, rendered via ToolViewModel.

    Returns the same card payload the dashboard tool-detail surface consumes
    (spec §4.1 — one view-model, Rich + card). Pure read; no mutation.
    """
    from systemu.interface.command.view_models import ToolViewModel

    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        return CommandResult(status=CommandStatus.ERROR,
                             summary=f"Tool {tool_id!r} not found in vault.")

    # The vault returns a Tool model (attributes); ToolViewModel.from_header
    # expects a header dict. Build one from the tool object, coercing the
    # ToolStatus enum to its string value for clean JSON.
    status = getattr(tool, "status", "")
    header = {
        "id": getattr(tool, "id", ""),
        "name": getattr(tool, "name", ""),
        "tool_type": getattr(tool, "tool_type", "—"),
        "status": getattr(status, "value", status),
        "description": getattr(tool, "description", "") or "",
        "enabled": bool(getattr(tool, "enabled", False)),
        "dry_run_status": getattr(tool, "dry_run_status", "not_run"),
    }
    vm = ToolViewModel.from_header(header)
    return CommandResult(status=CommandStatus.OK,
                         summary=f"Tool {vm.name} ({vm.status}).",
                         data={"card": vm.to_card(), "row": vm.to_row()})


def tools_recalibrate(tool_id: str, *, reason: str, vault) -> CommandResult:
    """Bump a tool's version and append a 'bump' entry to evolution_history.

    Canonical writer for the lightweight (no-code-change) recalibration path.
    Mirrors the models.py evolution_history contract (line 302):
      {"version": int, "reason": str, "mode": "bump"|"fork",
       "diff_summary": str, "ts": iso}.
    """
    from datetime import datetime, timezone

    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        return CommandResult(status=CommandStatus.ERROR,
                             summary=f"Tool {tool_id!r} not found in vault.")

    new_version = int(getattr(tool, "version", 1)) + 1
    tool.version = new_version
    if not getattr(tool, "evolution_history", None):
        tool.evolution_history = []
    tool.evolution_history.append({
        "version": new_version,
        "reason": reason,
        "mode": "bump",
        "diff_summary": "",
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    })
    vault.save_tool(tool)
    return CommandResult(
        status=CommandStatus.OK,
        summary=f"Recalibrated {getattr(tool, 'name', tool_id)} → v{new_version} ({reason}).",
        data={"tool_id": tool_id, "version": new_version},
    )


# ── settings_set ────────────────────────────────────────────────────────────
# Allow-list of operator-writable settings keys. Each maps to a real env var
# the Settings page persists via _update_env_var (see pages/settings.py).
_WRITABLE_SETTINGS = {"non_interactive", "evolution_schedule_cadence", "gate_mode"}

_SETTING_ENV_KEYS = {
    "non_interactive":            "SYSTEMU_NON_INTERACTIVE",
    "evolution_schedule_cadence": "SYSTEMU_EVOLUTION_SCHEDULE_CADENCE",
    "gate_mode":                  "SYSTEMU_GATE_MODE",
}


def _persist_setting(key: str, value: str) -> None:
    """Persist one allow-listed setting to the real backing store.

    Routes through the Settings page's own low-level env writer
    (pages/settings.py:_update_env_var), which is the actual persistence the
    GUI Settings surface uses — writes the key to .env and is read back from
    os.environ on the next boot. Tests monkeypatch this seam.
    """
    import os
    from systemu.interface.pages.settings import _update_env_var

    env_key = _SETTING_ENV_KEYS[key]
    _update_env_var(env_key, value)
    os.environ[env_key] = value


def doctor_apply(actions, *, vault) -> CommandResult:
    """Apply a list of RecoveryActions through the shared recovery dispatchers.

    This is the headless counterpart to the web recovery panel: it calls the
    SAME ``recover.py:_handle_action`` (threading the CLI's own vault) so both
    surfaces share ONE apply path. Auto-applyable kinds — DEP_PENDING,
    GATE_3_DISABLED, MEMORY_POISONED — are dispatched; gate-review kinds
    (GATE_1_PENDING / GATE_2_PENDING / etc.) are reported as skipped (they
    require an operator gate review and route via fix_url, not here).

    Exceptions from individual dispatches are surfaced in the log (NOT
    swallowed) and counted as failures. Status is OK if any action applied,
    else NOOP (nothing was applyable / nothing to do).
    """
    from systemu.interface.pages.recover import _handle_action

    _APPLYABLE = {"DEP_PENDING", "GATE_3_DISABLED", "MEMORY_POISONED"}

    log: list[str] = []
    applied = 0
    failed = 0
    for a in actions:
        label = f"{a.scope_kind} {a.scope_id}: {a.kind}"
        if a.kind not in _APPLYABLE:
            log.append(f"Skipped (manual gate): {label}")
            continue
        try:
            _handle_action(a, vault=vault)
            applied += 1
            log.append(f"Applied: {label}")
        except Exception as exc:  # surface, do not swallow
            failed += 1
            log.append(f"FAILED: {label} -- {exc}")

    if applied:
        status = CommandStatus.OK
        summary = f"Applied {applied} action(s)" + (
            f", {failed} failed" if failed else "")
    elif failed:
        status = CommandStatus.ERROR
        summary = f"Applied 0 action(s), {failed} failed"
    else:
        status = CommandStatus.NOOP
        summary = "No applyable actions (manual gates only / nothing to do)."

    return CommandResult(status=status, summary=summary,
                         data={"applied": applied, "failed": failed, "log": log})


def settings_set(key: str, value: str, *, vault) -> CommandResult:
    """Set a single allow-listed setting. Canonical writer for operator settings.

    Guarded by ``_WRITABLE_SETTINGS`` — unknown keys are rejected with an
    ERROR result rather than silently writing arbitrary env vars. ``vault`` is
    accepted for signature uniformity with the other verbs but unused.
    """
    if key not in _WRITABLE_SETTINGS:
        allowed = ", ".join(sorted(_WRITABLE_SETTINGS))
        return CommandResult(
            status=CommandStatus.ERROR,
            summary=f"Unknown setting {key!r}. Writable settings: {allowed}.",
        )
    try:
        _persist_setting(key, value)
    except Exception as exc:  # surface writer failures as ERROR, not a crash
        return CommandResult(status=CommandStatus.ERROR,
                             summary=f"Failed to persist {key}: {exc}")
    return CommandResult(status=CommandStatus.OK,
                         summary=f"Set {key} = {value}.",
                         data={"key": key, "value": value})
