"""Item 1 — the fleet gives each child an isolated per-child audit namespace."""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from systemu.runtime.subagent_fleet import SubagentFleet


@pytest.mark.asyncio
async def test_fleet_passes_isolated_audit_namespace_to_child():
    cfg = MagicMock()
    cfg.delegate_max_concurrent_children = 2
    vault = MagicMock()
    sentinel = Path("/tmp/child-ns-0")
    vault.create_child_execution_namespace.return_value = sentinel

    captured = {}

    class FakeRuntime:
        def __init__(self, config, vault, audit_namespace=None):
            captured["audit_namespace"] = audit_namespace

        async def execute(self, shadow, activity, origin=None, root_execution_id=None):
            return {"status": "success", "summary": "ok", "tool_calls": 1}

    with patch("systemu.runtime.shadow_runtime.ShadowRuntime", FakeRuntime), \
         patch("systemu.runtime.subagent_fleet.build_child_shadow",
               lambda parent, cid: MagicMock()), \
         patch("systemu.runtime.subagent_fleet.build_child_activity",
               lambda pa, task, cid, v: MagicMock()):
        fleet = SubagentFleet(parent_execution_id="p1", config=cfg, vault=vault)
        await fleet.spawn_children(MagicMock(), MagicMock(), ["t1"])

    assert captured.get("audit_namespace") == sentinel
    vault.create_child_execution_namespace.assert_called_once_with("p1", "child-0")


@pytest.mark.asyncio
async def test_fleet_tolerates_namespace_failure():
    """If namespace creation fails, the child still runs (audit_namespace=None)."""
    cfg = MagicMock()
    cfg.delegate_max_concurrent_children = 2
    vault = MagicMock()
    vault.create_child_execution_namespace.side_effect = RuntimeError("disk full")
    captured = {}

    class FakeRuntime:
        def __init__(self, config, vault, audit_namespace=None):
            captured["audit_namespace"] = audit_namespace

        async def execute(self, shadow, activity, origin=None, root_execution_id=None):
            return {"status": "success", "summary": "ok"}

    with patch("systemu.runtime.shadow_runtime.ShadowRuntime", FakeRuntime), \
         patch("systemu.runtime.subagent_fleet.build_child_shadow",
               lambda parent, cid: MagicMock()), \
         patch("systemu.runtime.subagent_fleet.build_child_activity",
               lambda pa, task, cid, v: MagicMock()):
        fleet = SubagentFleet(parent_execution_id="p1", config=cfg, vault=vault)
        res = await fleet.spawn_children(MagicMock(), MagicMock(), ["t1"])

    assert captured.get("audit_namespace") is None      # fell back, didn't crash
    assert res["any_succeeded"] is True
