"""Regression tests for v0.8.4 — bundles three UX-correctness fixes from the
post-v0.8.3 "default path = failure path" audit:

  1. Workshop's rebuild_scroll now runs validate_and_propose_tools (Pattern 3
     extended to the Workshop UI path).  Pre-v0.8.4 Workshop bypassed the
     validator entirely.

  2. Evolution APPROVED → auto-applies via apply_evolution() so the APPROVED
     status isn't a dead-end state.

  3. OperatorDecisionQueue.get_resolved_choice WARNs on corrupt vault entries
     instead of silently re-prompting the operator.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fix #1: workshop_module.rebuild_scroll runs validate_and_propose_tools
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_and_propose_tools_helper_exists_at_module_scope():
    """The extracted helper must be importable from scroll_refiner."""
    from systemu.pipelines.scroll_refiner import validate_and_propose_tools
    assert callable(validate_and_propose_tools)


def test_workshop_rebuild_calls_validate_and_propose_tools(monkeypatch):
    """Workshop's rebuild_scroll must invoke the validator+propose-bridge
    on the rebuilt content (was bypassed pre-v0.8.4)."""
    import asyncio
    from systemu.core.models import Scroll, ScrollStatus
    from systemu.pipelines import workshop_module

    fake_scroll_dict = {
        "id": "scroll_test",
        "name": "Test Weather",
        "source_session_id": "test",
        "raw_instructions_path": "",
        "narrative_md": "test narrative",
        "objectives": [],
        "tags": [],
        "intent": "",
        "expected_outcome": "",
        "constraints": {},
        "pipeline_trace": [],
        "status": "pending_approval",
        "action_blocks": [],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "activity_id": "",
        "has_warnings": False,
    }

    async def _stub_llm(*args, **kwargs):
        return fake_scroll_dict

    monkeypatch.setattr(workshop_module, "async_llm_call_json", _stub_llm)

    monkeypatch.setattr(
        "systemu.pipelines.scroll_validator.is_enabled",
        lambda config: True,
    )

    satisfiable_result = MagicMock()
    satisfiable_result.satisfiable = True
    satisfiable_result.missing_tool_specs = []
    spy = MagicMock(return_value=satisfiable_result)
    monkeypatch.setattr(
        "systemu.pipelines.scroll_refiner.validate_and_propose_tools",
        spy,
    )

    existing_scroll = Scroll(
        id="scroll_test", name="Old", source_session_id="x",
        raw_instructions_path="", narrative_md="old",
        status=ScrollStatus.VALIDATOR_BLOCKED,
    )
    fake_vault = MagicMock()
    fake_vault.get_scroll.return_value = existing_scroll
    fake_vault.list_pending_notifications.return_value = []

    asyncio.run(workshop_module.rebuild_scroll(
        scroll_id="scroll_test",
        prompt="try again",
        config=MagicMock(),
        vault=fake_vault,
    ))

    assert spy.called, (
        "workshop_module.rebuild_scroll did NOT call validate_and_propose_tools — "
        "the v0.8.4 fix is not wired"
    )


def test_workshop_rebuild_sets_validator_blocked_when_blocked(monkeypatch):
    """When validator says rebuilt scroll is still blocked, Workshop must
    set status=VALIDATOR_BLOCKED, NOT PENDING_APPROVAL."""
    import asyncio
    from systemu.core.models import Scroll, ScrollStatus
    from systemu.pipelines import workshop_module

    fake_scroll_dict = {
        "id": "scroll_blk", "name": "Blocked",
        "source_session_id": "x", "raw_instructions_path": "",
        "narrative_md": "n", "objectives": [], "tags": [],
        "intent": "", "expected_outcome": "", "constraints": {},
        "pipeline_trace": [], "status": "pending_approval",
        "action_blocks": [], "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00", "activity_id": "",
        "has_warnings": False,
    }

    async def _stub_llm(*args, **kwargs):
        return fake_scroll_dict

    monkeypatch.setattr(workshop_module, "async_llm_call_json", _stub_llm)
    monkeypatch.setattr(
        "systemu.pipelines.scroll_validator.is_enabled",
        lambda config: True,
    )

    blocked_result = MagicMock()
    blocked_result.satisfiable = False
    blocked_result.missing_tool_specs = []
    monkeypatch.setattr(
        "systemu.pipelines.scroll_refiner.validate_and_propose_tools",
        MagicMock(return_value=blocked_result),
    )

    existing = Scroll(
        id="scroll_blk", name="Old", source_session_id="x",
        raw_instructions_path="", narrative_md="old",
        status=ScrollStatus.VALIDATOR_BLOCKED,
    )
    fake_vault = MagicMock()
    fake_vault.get_scroll.return_value = existing
    fake_vault.list_pending_notifications.return_value = []

    result = asyncio.run(workshop_module.rebuild_scroll(
        scroll_id="scroll_blk",
        prompt="retry",
        config=MagicMock(),
        vault=fake_vault,
    ))

    assert result.status == ScrollStatus.VALIDATOR_BLOCKED, (
        f"expected VALIDATOR_BLOCKED, got {result.status}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fix #2: Evolution APPROVED → auto-applies
# ─────────────────────────────────────────────────────────────────────────────

def test_evolution_approve_triggers_auto_apply(monkeypatch):
    """When operator approves an evolution, apply_evolution must be called
    right away (was deferred indefinitely pre-v0.8.4)."""
    from systemu.pipelines import evolution_engine
    from systemu.core.models import (
        Evolution, EvolutionStatus, EvolutionType,
    )

    fake_evolution = Evolution(
        id="evo_test",
        evolution_type=EvolutionType.UPGRADE,
        target_entity_type="shadow",
        target_entity_ids=["shadow_x"],
        description="boost",
        rationale="reason",
        status=EvolutionStatus.PROPOSED,
    )

    fake_vault = MagicMock()
    fake_vault.get_evolution.return_value = fake_evolution
    fake_config = MagicMock()

    monkeypatch.setattr(
        evolution_engine, "notify_user",
        MagicMock(return_value="approve"),
    )

    apply_spy = MagicMock(return_value=True)
    monkeypatch.setattr(evolution_engine, "apply_evolution", apply_spy)

    evolution_engine._notify_evolution(fake_evolution, fake_config, fake_vault)

    assert apply_spy.called, (
        "After Approve, apply_evolution was NOT called — "
        "the v0.8.4 auto-apply fix is not wired"
    )
    apply_spy.assert_called_once_with("evo_test", fake_config, fake_vault)


def test_evolution_reject_does_not_trigger_apply(monkeypatch):
    """Rejection must NOT call apply_evolution."""
    from systemu.pipelines import evolution_engine
    from systemu.core.models import (
        Evolution, EvolutionStatus, EvolutionType,
    )

    fake_evolution = Evolution(
        id="evo_test",
        evolution_type=EvolutionType.UPGRADE,
        target_entity_type="shadow",
        target_entity_ids=["shadow_x"],
        description="boost",
        rationale="reason",
        status=EvolutionStatus.PROPOSED,
    )

    fake_vault = MagicMock()
    fake_vault.get_evolution.return_value = fake_evolution

    monkeypatch.setattr(
        evolution_engine, "notify_user",
        MagicMock(return_value="reject"),
    )
    apply_spy = MagicMock()
    monkeypatch.setattr(evolution_engine, "apply_evolution", apply_spy)

    evolution_engine._notify_evolution(fake_evolution, MagicMock(), fake_vault)

    apply_spy.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Fix #3: decision_queue.get_resolved_choice WARNs on corrupt skip
# ─────────────────────────────────────────────────────────────────────────────

def test_get_resolved_choice_warns_when_body_missing(caplog):
    """When the index has a resolved header but the body file is corrupt/missing,
    log a WARNING (not silent continue)."""
    import logging
    from systemu.approval.decision_queue import OperatorDecisionQueue

    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [{
        "id": "dec_corrupt",
        "dedup_key": "test:key",
        "status": "resolved",
        "created_at": "2026-01-01T00:00:00",
    }]
    fake_vault.get_decision.side_effect = KeyError("body not found")

    queue = OperatorDecisionQueue(fake_vault)

    with caplog.at_level(logging.WARNING, logger="systemu.approval.decision_queue"):
        result = queue.get_resolved_choice("test:key")

    assert result is None, "must return None when body unloadable"
    warning_logs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("dec_corrupt" in (r.message + " " + str(r.args))
               for r in warning_logs), (
        f"expected WARNING about corrupt header dec_corrupt, "
        f"got: {[r.message for r in warning_logs]}"
    )


def test_get_resolved_choice_returns_choice_normally(caplog):
    """Smoke test: healthy vault returns choice + emits no warnings."""
    import logging
    from systemu.approval.decision_queue import OperatorDecisionQueue, OperatorDecision
    from datetime import datetime, timezone

    fake_decision = OperatorDecision(
        id="dec_good", title="t", body="b",
        options=["Skip", "Forge"], context={},
        dedup_key="test:ok",
        status="resolved", choice="Forge",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        resolved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [{
        "id": "dec_good", "dedup_key": "test:ok", "status": "resolved",
        "created_at": "2026-01-01T00:00:00",
    }]
    fake_vault.get_decision.return_value = fake_decision

    queue = OperatorDecisionQueue(fake_vault)

    with caplog.at_level(logging.WARNING, logger="systemu.approval.decision_queue"):
        result = queue.get_resolved_choice("test:ok")

    assert result == "Forge"
    warning_logs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_logs) == 0, f"unexpected warnings: {[r.message for r in warning_logs]}"
