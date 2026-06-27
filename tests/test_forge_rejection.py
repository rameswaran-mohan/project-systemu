"""v0.9.49 F2 — declining a forge gate finalizes the dependent parked activities.

Verified repro: operator Skips the `forge:<tool_id>` gate for the 2nd tool; the
PARTIAL activity that required it used to hang `waiting_on_tools` forever. Now
resolve_gate flags the tool `forge_rejected` and finalizes every PARTIAL activity
that required it (ANY-unavailable rule, idempotent — both inherited from F1).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from systemu.core.models import (
    Activity, ActivityStatus, Tool, ToolStatus, ToolType,
)
from systemu.interface.command.result import CommandStatus


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _tool(vault, tid, *, status=ToolStatus.PROPOSED, dry_run_status="not_run"):
    vault.save_tool(Tool(id=tid, name=tid, description="d", tool_type=ToolType.PYTHON_FUNCTION,
                         status=status, implementation_path=f"p/{tid}.py",
                         parameters_schema={}, dry_run_status=dry_run_status))


def _activity(vault, aid, required):
    vault.save_activity(Activity(id=aid, name="task", scroll_id="s",
                                 required_tool_ids=list(required),
                                 status=ActivityStatus.PARTIAL))


def _forge_skip(tool_id, choice="Skip"):
    return SimpleNamespace(context={"gate_type": "forge"}, choice=choice,
                           dedup_key=f"forge:{tool_id}")


def test_forge_skip_flags_tool_and_finalizes_activity(vault):
    from systemu.interface.command.inbox import resolve_gate
    _tool(vault, "t_arc")
    _activity(vault, "a1", ["t_arc"])
    res = resolve_gate(_forge_skip("t_arc"), vault=vault)
    assert vault.get_tool("t_arc").forge_rejected is True
    assert vault.get_activity("a1").status == ActivityStatus.FAILED
    assert res.status == CommandStatus.OK and "t_arc" in res.summary


def test_forge_skip_multitool_any_rule(vault):
    # repro shape: one deployed tool + one rejected → activity still finalizes
    from systemu.interface.command.inbox import resolve_gate
    _tool(vault, "hash", status=ToolStatus.DEPLOYED, dry_run_status="passed")
    _tool(vault, "arc")
    _activity(vault, "a2", ["hash", "arc"])
    resolve_gate(_forge_skip("arc"), vault=vault)
    assert vault.get_activity("a2").status == ActivityStatus.FAILED


def test_forge_skip_no_dependent_activity_is_noop(vault):
    from systemu.interface.command.inbox import resolve_gate
    _tool(vault, "t_lonely")
    res = resolve_gate(_forge_skip("t_lonely"), vault=vault)
    assert vault.get_tool("t_lonely").forge_rejected is True      # still flagged
    assert res.status == CommandStatus.NOOP


def test_forge_skip_malformed_dedup_does_not_crash(vault):
    from systemu.interface.command.inbox import resolve_gate
    res = resolve_gate(SimpleNamespace(context={"gate_type": "forge"},
                                       choice="Skip", dedup_key="forge:"), vault=vault)
    assert res.status == CommandStatus.NOOP


def test_forge_approve_still_routes_to_forge_not_reject(vault):
    # choice 'Forge' (an approve label) must NOT hit the reject hook
    from systemu.interface.command.inbox import resolve_gate
    _tool(vault, "t_go")
    with patch("systemu.pipelines.tool_forge.forge_tool_from_spec") as forge, \
         patch("sharing_on.config.Config.from_env"):
        res = resolve_gate(_forge_skip("t_go", choice="Forge"), vault=vault)
    forge.assert_called_once()
    assert vault.get_tool("t_go").forge_rejected is False
    assert res.status == CommandStatus.OK


# ── F3: tools_blocked "Enable & run" on an un-enable-able tool finalizes ──────

def _tools_blocked(activity_id, tool_ids):
    return SimpleNamespace(
        context={"gate_type": "tools_blocked", "tool_ids": list(tool_ids),
                 "activity_id": activity_id},
        choice="Enable & run", dedup_key=f"tools_blocked:{activity_id}")


def test_enable_run_on_declined_tool_finalizes(vault):
    from systemu.interface.command.inbox import resolve_gate
    _tool(vault, "arc")
    t = vault.get_tool("arc"); t.forge_rejected = True; vault.save_tool(t)
    _activity(vault, "a", ["arc"])
    err = SimpleNamespace(status=CommandStatus.ERROR, summary="cannot enable", data=None)
    with patch("systemu.interface.command.verbs.tools_enable", return_value=err), \
         patch("systemu.interface.notifications.log_event"):
        res = resolve_gate(_tools_blocked("a", ["arc"]), vault=vault)
    assert vault.get_activity("a").status == ActivityStatus.FAILED
    assert res.status == CommandStatus.ERROR


def test_enable_run_happy_path_does_not_finalize(vault):
    from systemu.interface.command.inbox import resolve_gate
    _tool(vault, "ok", status=ToolStatus.DEPLOYED, dry_run_status="passed")
    _activity(vault, "a", ["ok"])
    ok = SimpleNamespace(status=CommandStatus.OK, summary="enabled", data={"tool_id": "ok"})
    with patch("systemu.interface.command.verbs.tools_enable", return_value=ok), \
         patch("systemu.pipelines.tool_service.heal_activities_for_tool"), \
         patch("sharing_on.config.Config.from_env"):
        res = resolve_gate(_tools_blocked("a", ["ok"]), vault=vault)
    assert vault.get_activity("a").status == ActivityStatus.PARTIAL    # NOT finalized
    assert res.status == CommandStatus.OK
