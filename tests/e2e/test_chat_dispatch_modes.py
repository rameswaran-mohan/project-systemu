"""E2E: chat-page dispatch radio → direct_task pipeline mapping.

Headless (no NiceGUI server) — calls the page module's helpers directly and
asserts the radio default plus the direct_task flag wiring.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


def test_default_dispatch_local_runs_synchronously(monkeypatch):
    monkeypatch.setenv("SYSTEMU_MODE", "local")
    from systemu.interface.pages.chat_page import _default_dispatch_mode
    assert _default_dispatch_mode() == "run_now"


def test_default_dispatch_docker_local_queues(monkeypatch):
    monkeypatch.setenv("SYSTEMU_MODE", "docker-local")
    from systemu.interface.pages.chat_page import _default_dispatch_mode
    assert _default_dispatch_mode() == "queue"


def test_default_dispatch_docker_enterprise_queues(monkeypatch):
    monkeypatch.setenv("SYSTEMU_MODE", "docker-enterprise")
    from systemu.interface.pages.chat_page import _default_dispatch_mode
    assert _default_dispatch_mode() == "queue"


def test_default_dispatch_unknown_mode_falls_back_to_run_now(monkeypatch):
    monkeypatch.setenv("SYSTEMU_MODE", "weird-future-mode")
    from systemu.interface.pages.chat_page import _default_dispatch_mode
    # "docker-*" prefix triggers queue; anything else is run_now.
    assert _default_dispatch_mode() == "run_now"


# ── direct_task: synchronous vs queued path ────────────────────────────────

def test_direct_task_run_now_does_not_call_supervisor(minimal_vault, real_config):
    """route_through_supervisor=False must call ShadowRuntime directly,
    NOT touch the Supervisor singleton."""
    from systemu.core.models import (
        Activity, Shadow, ShadowStatus, Scroll, Objective,
    )
    shadow = Shadow(id="s1", name="t", description="t", system_prompt="t",
                    status=ShadowStatus.AWAKENED)
    minimal_vault.save_shadow(shadow)
    scroll = Scroll(id="sc1", name="t", source_session_id="x",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="g", success_criteria="c")])
    minimal_vault.save_scroll(scroll)
    activity = Activity(id="a1", name="t", scroll_id=scroll.id,
                        required_tool_ids=[], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    minimal_vault.save_activity(activity)

    fake_runtime = MagicMock()
    fake_runtime.execute = MagicMock(return_value={
        "status": "success", "execution_id": "e1",
    })

    # Patch every stage 1-4 component so we exercise just the dispatch branch.
    with patch("systemu.pipelines.scroll_refiner.refine_from_text", return_value=scroll), \
         patch("systemu.pipelines.activity_extractor.extract_and_process", return_value=activity), \
         patch("systemu.pipelines.shadow_decision.decide_shadow", return_value=shadow), \
         patch("systemu.pipelines.activity_extractor.init_pipeline"), \
         patch("systemu.runtime.shadow_runtime.ShadowRuntime", return_value=fake_runtime), \
         patch("systemu.core.llm_router._run_coroutine",
               side_effect=lambda coro: {"status": "success", "execution_id": "e1"}), \
         patch("systemu.runtime.supervisor.Supervisor.get") as get_sup:

        from systemu.pipelines.direct_task import run_direct_task
        result = run_direct_task("hello", real_config, minimal_vault,
                                 route_through_supervisor=False)

    assert result is activity
    # Supervisor.get should NOT have been called in run-now mode
    assert get_sup.call_count == 0


def test_direct_task_queue_mode_calls_supervisor_submit(minimal_vault, real_config):
    from systemu.core.models import (
        Activity, Shadow, ShadowStatus, Scroll, Objective,
    )
    shadow = Shadow(id="sQ", name="t", description="t", system_prompt="t",
                    status=ShadowStatus.AWAKENED)
    minimal_vault.save_shadow(shadow)
    scroll = Scroll(id="scQ", name="t", source_session_id="x",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="g", success_criteria="c")])
    minimal_vault.save_scroll(scroll)
    activity = Activity(id="aQ", name="t", scroll_id=scroll.id,
                        required_tool_ids=[], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    minimal_vault.save_activity(activity)

    fake_supervisor = MagicMock()
    fake_supervisor.submit = MagicMock(return_value="sub_xyz")

    with patch("systemu.pipelines.scroll_refiner.refine_from_text", return_value=scroll), \
         patch("systemu.pipelines.activity_extractor.extract_and_process", return_value=activity), \
         patch("systemu.pipelines.shadow_decision.decide_shadow", return_value=shadow), \
         patch("systemu.pipelines.activity_extractor.init_pipeline"), \
         patch("systemu.runtime.supervisor.Supervisor.get", return_value=fake_supervisor):

        from systemu.pipelines.direct_task import run_direct_task
        result = run_direct_task("hello", real_config, minimal_vault,
                                 route_through_supervisor=True)

    assert result is activity
    fake_supervisor.submit.assert_called_once()
    args, kwargs = fake_supervisor.submit.call_args
    assert kwargs.get("reason") == "chat"


def test_direct_task_queue_mode_handles_uninitialised_supervisor(
    minimal_vault, real_config,
):
    """When Supervisor isn't running in this process, give a friendly error
    rather than letting RuntimeError bubble up to the chat UI."""
    from systemu.core.models import (
        Activity, Shadow, ShadowStatus, Scroll, Objective,
    )
    shadow = Shadow(id="sU", name="t", description="t", system_prompt="t",
                    status=ShadowStatus.AWAKENED)
    minimal_vault.save_shadow(shadow)
    scroll = Scroll(id="scU", name="t", source_session_id="x",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="g", success_criteria="c")])
    minimal_vault.save_scroll(scroll)
    activity = Activity(id="aU", name="t", scroll_id=scroll.id,
                        required_tool_ids=[], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    minimal_vault.save_activity(activity)

    with patch("systemu.pipelines.scroll_refiner.refine_from_text", return_value=scroll), \
         patch("systemu.pipelines.activity_extractor.extract_and_process", return_value=activity), \
         patch("systemu.pipelines.shadow_decision.decide_shadow", return_value=shadow), \
         patch("systemu.pipelines.activity_extractor.init_pipeline"), \
         patch("systemu.runtime.supervisor.Supervisor.get",
               side_effect=RuntimeError("Supervisor not initialised")):

        from systemu.pipelines.direct_task import run_direct_task
        result = run_direct_task("hello", real_config, minimal_vault,
                                 route_through_supervisor=True)

    # No exception bubbles out; the chat history shows a failed entry.
    assert result is activity
    history = minimal_vault.load_chat_history(limit=5)
    assert any("not running" in (e.get("error") or "") for e in history)
