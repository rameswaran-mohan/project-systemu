"""v0.8.5 bundle regression tests."""
from __future__ import annotations
from unittest.mock import MagicMock
import pytest


# ─── Fix A: DecisionDispatcher ─────────────────────────────────────────────

class TestDispatcher:
    def test_register_and_dispatch_calls_handler(self):
        from systemu.approval.decision_dispatcher import register, dispatch, _HANDLERS
        from systemu.approval.decision_queue import OperatorDecision
        import systemu.approval.decision_dispatcher as _disp_mod

        _HANDLERS.clear()
        _disp_mod._handlers_bootstrapped = True  # suppress eager bootstrap
        spy = MagicMock()
        register("test_ns", spy)

        decision = OperatorDecision(
            id="dec_1", title="t", body="b", options=["a"],
            dedup_key="test_ns:foo",
        )
        config = MagicMock()
        vault = MagicMock()
        dispatch(decision, "a", config, vault)
        spy.assert_called_once_with(decision, "a", config, vault)

    def test_unknown_namespace_is_no_op(self, caplog):
        from systemu.approval.decision_dispatcher import dispatch, _HANDLERS
        from systemu.approval.decision_queue import OperatorDecision
        import systemu.approval.decision_dispatcher as _disp_mod

        _HANDLERS.clear()
        _disp_mod._handlers_bootstrapped = True  # suppress eager bootstrap
        decision = OperatorDecision(
            id="dec_2", title="t", body="b", options=["x"],
            dedup_key="nonexistent:foo",
        )
        # Must not raise, must not call any handler
        dispatch(decision, "x", MagicMock(), MagicMock())

    def test_handler_exception_is_logged_not_propagated(self, caplog):
        import logging
        from systemu.approval.decision_dispatcher import register, dispatch, _HANDLERS
        from systemu.approval.decision_queue import OperatorDecision
        import systemu.approval.decision_dispatcher as _disp_mod

        _HANDLERS.clear()
        _disp_mod._handlers_bootstrapped = True  # suppress eager bootstrap
        def _bad(*args, **kwargs):
            raise RuntimeError("boom")
        register("boom_ns", _bad)

        decision = OperatorDecision(
            id="dec_3", title="t", body="b", options=["x"],
            dedup_key="boom_ns:foo",
        )
        with caplog.at_level(logging.ERROR, logger="systemu.approval.decision_dispatcher"):
            dispatch(decision, "x", MagicMock(), MagicMock())
        assert any("boom_ns" in r.message for r in caplog.records)

    def test_shadow_decision_handler_calls_decide_shadow(self, monkeypatch):
        """shadow_decision: namespace handler must fetch activity and call decide_shadow."""
        from systemu.approval.decision_queue import OperatorDecision
        # Importing shadow_decision triggers register('shadow_decision', _handler)
        from systemu.pipelines import shadow_decision as sd
        from systemu.approval.decision_dispatcher import _HANDLERS

        spy = MagicMock()
        monkeypatch.setattr(sd, "decide_shadow", spy)

        fake_activity = MagicMock(id="act_123")
        fake_vault = MagicMock()
        fake_vault.get_activity.return_value = fake_activity

        decision = OperatorDecision(
            id="dec_4", title="t", body="b", options=["Awaken"],
            dedup_key="shadow_decision:act_123",
        )
        handler = _HANDLERS.get("shadow_decision")
        assert handler is not None, "shadow_decision handler not registered"
        handler(decision, "Awaken", MagicMock(), fake_vault)
        fake_vault.get_activity.assert_called_once_with("act_123")
        spy.assert_called_once()
        # decide_shadow's first positional arg is the activity
        assert spy.call_args.args[0] is fake_activity

    def test_validator_propose_forge_all_invokes_forge_pipeline(self, monkeypatch):
        """validator_propose: namespace on 'Forge All' must call
        forge_proposed_tools_from_specs for the referenced scroll."""
        from systemu.approval.decision_queue import OperatorDecision
        from systemu.pipelines import scroll_refiner as sr  # ensures registration
        from systemu.approval.decision_dispatcher import _HANDLERS

        spy = MagicMock()
        monkeypatch.setattr("systemu.pipelines.tool_forge.forge_proposed_tools_from_specs", spy)

        fake_scroll = MagicMock(id="scroll_xyz", missing_tool_specs=[{"name": "t"}])
        fake_vault = MagicMock()
        fake_vault.get_scroll.return_value = fake_scroll

        decision = OperatorDecision(
            id="dec_10", title="t", body="b", options=["Forge All", "Skip"],
            dedup_key="validator_propose:scroll_xyz",
            context={"missing_tool_specs": [{"name": "t"}]},
        )
        handler = _HANDLERS.get("validator_propose")
        assert handler is not None
        handler(decision, "Forge All", MagicMock(), fake_vault)
        assert spy.called

    def test_validator_propose_skip_is_noop(self, monkeypatch):
        from systemu.approval.decision_queue import OperatorDecision
        from systemu.pipelines import scroll_refiner as sr  # ensures registration
        from systemu.approval.decision_dispatcher import _HANDLERS

        spy = MagicMock()
        monkeypatch.setattr("systemu.pipelines.tool_forge.forge_proposed_tools_from_specs", spy)

        decision = OperatorDecision(
            id="dec_11", title="t", body="b", options=["Forge All", "Skip"],
            dedup_key="validator_propose:scroll_xyz",
        )
        handler = _HANDLERS["validator_propose"]
        handler(decision, "Skip", MagicMock(), MagicMock())
        spy.assert_not_called()

    def test_forge_tool_handler_calls_forge_pipeline(self, monkeypatch):
        """forge_tool: namespace on 'Forge' must call forge_tool_from_spec()
        for the referenced tool_id (replays the existing UI-approved forge path)."""
        from systemu.approval.decision_queue import OperatorDecision
        from systemu.pipelines import tool_forge as tf
        from systemu.approval.decision_dispatcher import _HANDLERS

        spy = MagicMock()
        monkeypatch.setattr(tf, "forge_tool_from_spec", spy)

        fake_tool = MagicMock()
        fake_tool.model_dump_json.return_value = '{"name": "t"}'
        fake_vault = MagicMock()
        fake_vault.get_tool.return_value = fake_tool

        decision = OperatorDecision(
            id="dec_12", title="t", body="b", options=["Forge", "Skip"],
            dedup_key="forge_tool:tool_abc",
        )
        handler = _HANDLERS.get("forge_tool")
        assert handler is not None
        handler(decision, "Forge", MagicMock(), fake_vault)
        spy.assert_called_once()
        # tool_id should be the first positional arg
        assert spy.call_args.args[0] == "tool_abc"

    def test_dispatch_eagerly_registers_all_known_handlers(self):
        """dispatch() must trigger registration of all 3 pipeline handlers
        even if their modules weren't imported beforehand.

        Pre-fix: tool_forge had no import path from daemon boot, so forge_tool:*
        decisions resolved via dashboard silently no-op'd.
        """
        import sys
        from systemu.approval.decision_dispatcher import (
            dispatch, _HANDLERS, _ensure_handlers_registered,
        )
        from systemu.approval.decision_queue import OperatorDecision

        # Reset state to simulate fresh process: clear handler registry,
        # reset bootstrap flag, AND evict the pipeline modules so re-import
        # actually re-runs their module-level register() calls.
        _HANDLERS.clear()
        import systemu.approval.decision_dispatcher as _disp_mod
        _disp_mod._handlers_bootstrapped = False
        for _modname in (
            "systemu.pipelines.shadow_decision",
            "systemu.pipelines.scroll_refiner",
            "systemu.pipelines.tool_forge",
        ):
            sys.modules.pop(_modname, None)

        # Call dispatch with an unrelated namespace — should still trigger
        # the bootstrap, populating all 3 handlers.
        dispatch(
            OperatorDecision(
                id="dec_x", title="t", body="b", options=["x"],
                dedup_key="unknown_namespace:foo",
            ),
            "x", MagicMock(), MagicMock(),
        )
        assert "shadow_decision" in _HANDLERS, "shadow_decision handler not registered"
        assert "validator_propose" in _HANDLERS, "validator_propose handler not registered"
        assert "forge_tool" in _HANDLERS, "forge_tool handler not registered"


# ─── Fix C: Workshop conditional notification ──────────────────────────────

class TestWorkshopConditionalNotification:
    def test_blocked_scroll_queues_ok_card_not_approve(self, monkeypatch):
        """When validator still blocks rebuilt scroll, notification must NOT
        have an Approve action (would error silently when clicked)."""
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
        monkeypatch.setattr("systemu.pipelines.scroll_validator.is_enabled",
                            lambda config: True)

        blocked_result = MagicMock(satisfiable=False, missing_tool_specs=[])
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

        asyncio.run(workshop_module.rebuild_scroll(
            scroll_id="scroll_blk", prompt="retry",
            config=MagicMock(), vault=fake_vault,
        ))

        queued_calls = fake_vault.queue_notification.call_args_list
        assert len(queued_calls) == 1
        notif = queued_calls[0].args[0]
        assert notif.actions == ["OK"], (
            f"blocked scroll must queue OK-only card, got actions={notif.actions}"
        )
        assert notif.context.get("notification_type") == "scroll_blocked_info"
        assert "Blocked" in notif.title or "Tools Required" in notif.title

    def test_satisfiable_scroll_queues_approve_reject_card(self, monkeypatch):
        """When validator passes after rebuild, queue the existing Approve/Reject
        scroll_approval card (unchanged behavior)."""
        import asyncio
        from systemu.core.models import Scroll, ScrollStatus
        from systemu.pipelines import workshop_module

        fake_scroll_dict = {
            "id": "scroll_ok", "name": "Ok",
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
        monkeypatch.setattr("systemu.pipelines.scroll_validator.is_enabled",
                            lambda config: True)

        ok_result = MagicMock(satisfiable=True, missing_tool_specs=[])
        monkeypatch.setattr(
            "systemu.pipelines.scroll_refiner.validate_and_propose_tools",
            MagicMock(return_value=ok_result),
        )

        existing = Scroll(
            id="scroll_ok", name="Old", source_session_id="x",
            raw_instructions_path="", narrative_md="old",
            status=ScrollStatus.PENDING_APPROVAL,
        )
        fake_vault = MagicMock()
        fake_vault.get_scroll.return_value = existing
        fake_vault.list_pending_notifications.return_value = []

        asyncio.run(workshop_module.rebuild_scroll(
            scroll_id="scroll_ok", prompt="tweak",
            config=MagicMock(), vault=fake_vault,
        ))

        queued = fake_vault.queue_notification.call_args_list[-1].args[0]
        assert "Approve" in queued.actions
        assert "Reject" in queued.actions
        assert queued.context.get("notification_type") == "scroll_approval"


# ─── Fix D: tool_dry_run asyncio wrapper ───────────────────────────────────

class TestDryRunAsyncioWrapper:
    def test_run_coro_sync_from_running_event_loop(self):
        """_run_coro_sync must work when called from inside an event loop
        (the dashboard's NiceGUI loop).  Pre-v0.8.5 the code used
        asyncio.run() directly and raised:
            RuntimeError: asyncio.run() cannot be called from a running event loop
        """
        import asyncio
        from systemu.pipelines.tool_dry_run import _run_coro_sync

        async def _inner():
            return 42

        async def _outer():
            return _run_coro_sync(_inner())

        # If the wrapper still uses bare asyncio.run, this raises.
        result = asyncio.run(_outer())
        assert result == 42

    def test_run_coro_sync_from_no_loop(self):
        """Outside any event loop, _run_coro_sync still works (fast path)."""
        from systemu.pipelines.tool_dry_run import _run_coro_sync

        async def _inner():
            return "hello"

        assert _run_coro_sync(_inner()) == "hello"

    def test_run_coro_sync_propagates_exception(self):
        """Exceptions inside the coroutine must propagate to the caller."""
        import asyncio
        from systemu.pipelines.tool_dry_run import _run_coro_sync

        async def _bad():
            raise ValueError("inside-coro")

        with pytest.raises(ValueError, match="inside-coro"):
            _run_coro_sync(_bad())


# ─── Fix B Part 2: scroll status advance broadening ────────────────────────

class TestStatusAdvance:
    def test_validator_blocked_scroll_advances_to_linked(self):
        from systemu.core.models import Scroll, ScrollStatus
        from systemu.pipelines.shadow_decision import _advance_scroll_after_shadow_assignment

        scroll = Scroll(
            id="scroll_blk", name="x", source_session_id="s",
            raw_instructions_path="", narrative_md="n",
            status=ScrollStatus.VALIDATOR_BLOCKED,
        )
        fake_vault = MagicMock()
        fake_vault.get_scroll.return_value = scroll
        _advance_scroll_after_shadow_assignment("scroll_blk", fake_vault)
        assert scroll.status == ScrollStatus.LINKED
        fake_vault.save_scroll.assert_called_once_with(scroll)

    def test_active_scroll_advances_to_linked_preserved(self):
        from systemu.core.models import Scroll, ScrollStatus
        from systemu.pipelines.shadow_decision import _advance_scroll_after_shadow_assignment

        scroll = Scroll(
            id="scroll_act", name="x", source_session_id="s",
            raw_instructions_path="", narrative_md="n",
            status=ScrollStatus.ACTIVE,
        )
        fake_vault = MagicMock()
        fake_vault.get_scroll.return_value = scroll
        _advance_scroll_after_shadow_assignment("scroll_act", fake_vault)
        assert scroll.status == ScrollStatus.LINKED

    def test_already_linked_scroll_is_left_alone(self):
        from systemu.core.models import Scroll, ScrollStatus
        from systemu.pipelines.shadow_decision import _advance_scroll_after_shadow_assignment

        scroll = Scroll(
            id="scroll_lnk", name="x", source_session_id="s",
            raw_instructions_path="", narrative_md="n",
            status=ScrollStatus.LINKED,
        )
        fake_vault = MagicMock()
        fake_vault.get_scroll.return_value = scroll
        _advance_scroll_after_shadow_assignment("scroll_lnk", fake_vault)
        fake_vault.save_scroll.assert_not_called()


# ─── Fix B Part 1: re-validation hook on tool deploy ───────────────────────

class TestRevalidationHook:
    def test_revalidate_blocked_scrolls_advances_satisfiable_scroll(self, monkeypatch):
        """When a tool is deployed and an existing VALIDATOR_BLOCKED scroll
        becomes satisfiable, the helper must advance it to PENDING_APPROVAL."""
        from systemu.core.models import Scroll, ScrollStatus
        from systemu.pipelines.scroll_refiner import revalidate_blocked_scrolls_for_tool

        scroll = Scroll(
            id="scroll_blk", name="x", source_session_id="s",
            raw_instructions_path="", narrative_md="n",
            status=ScrollStatus.VALIDATOR_BLOCKED,
        )
        fake_vault = MagicMock()
        fake_vault.list_scrolls.return_value = [{"id": "scroll_blk"}]
        fake_vault.get_scroll.return_value = scroll

        ok_result = MagicMock(satisfiable=True, missing_tool_specs=[])
        monkeypatch.setattr(
            "systemu.pipelines.scroll_refiner.validate_and_propose_tools",
            MagicMock(return_value=ok_result),
        )

        count = revalidate_blocked_scrolls_for_tool(
            "tool_new", config=MagicMock(), vault=fake_vault,
        )
        assert count == 1
        assert scroll.status == ScrollStatus.PENDING_APPROVAL

    def test_revalidate_blocked_scrolls_leaves_still_blocked_alone(self, monkeypatch):
        """If revalidate still says not satisfiable, scroll stays blocked."""
        from systemu.core.models import Scroll, ScrollStatus
        from systemu.pipelines.scroll_refiner import revalidate_blocked_scrolls_for_tool

        scroll = Scroll(
            id="scroll_blk", name="x", source_session_id="s",
            raw_instructions_path="", narrative_md="n",
            status=ScrollStatus.VALIDATOR_BLOCKED,
        )
        fake_vault = MagicMock()
        fake_vault.list_scrolls.return_value = [{"id": "scroll_blk"}]
        fake_vault.get_scroll.return_value = scroll

        blocked_result = MagicMock(satisfiable=False, missing_tool_specs=[])
        monkeypatch.setattr(
            "systemu.pipelines.scroll_refiner.validate_and_propose_tools",
            MagicMock(return_value=blocked_result),
        )

        count = revalidate_blocked_scrolls_for_tool(
            "tool_new", config=MagicMock(), vault=fake_vault,
        )
        assert count == 0
        assert scroll.status == ScrollStatus.VALIDATOR_BLOCKED

    def test_heal_activities_for_tool_triggers_revalidation(self, monkeypatch):
        """tool_service.heal_activities_for_tool must call the helper."""
        from systemu.pipelines import tool_service

        spy = MagicMock(return_value=0)
        monkeypatch.setattr(
            "systemu.pipelines.scroll_refiner.revalidate_blocked_scrolls_for_tool",
            spy,
        )
        # Stub _heal_partial_activities so we isolate the revalidation call
        monkeypatch.setattr(tool_service, "_heal_partial_activities",
                            MagicMock())

        tool_service.heal_activities_for_tool(
            "tool_xyz", config=MagicMock(), vault=MagicMock(),
        )
        assert spy.called
        assert spy.call_args.args[0] == "tool_xyz"
