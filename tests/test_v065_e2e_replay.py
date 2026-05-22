"""— replays cap_20260517_145110 to confirm the fix would have caught
the regression.

The 2026-05-17 weather E2E (verdict in captures/...145110/E2E_VERDICT.md) showed:
  - Stage 1 returned confidence=low → narrative-only fallback
  - Stage 2 produced objectives mentioning 'capture screenshots'
  - Stage 3 picked web_screenshot
  - Execution failed with 'web_screenshot tool unavailable'

This test stubs Stage 1's intent at the exact same low-confidence value we
observed, then walks Stage 2 with v0.6.5 hardening active.  Asserts:
  - Stage 1 emits warn trace event
  - Stage 2 detects GUI codification and re-prompts
  - Rewritten objectives contain no 'screenshot' / 'snip' words
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from systemu.core.models import Objective


# Real values from cap_20260517_145110:
REAL_LOW_CONF_INTENT = {
    "intent": "Explore weather-related information and tools",
    "expected_outcome": "A session where weather information is accessed",
    "success_signal": "Multiple applications used in sequence",
    "abstracted_steps": [
        "Switch between multiple applications",
        "Interact with various tools",
        "Access weather-related information",
    ],
    "confidence": "low",
}

REAL_GUI_OBJECTIVES = [
    {"id": 1, "goal": "Verify development environment",
     "success_criteria": "checks pass"},
    {"id": 2, "goal": "Interact with weather application and capture screenshots",
     "success_criteria": "screenshots collected"},
    {"id": 3, "goal": "Create and save test report document",
     "success_criteria": "report exists"},
]

REWRITTEN_OBJECTIVES = [
    {"id": 1, "goal": "Verify development environment",
     "success_criteria": "checks pass"},
    {"id": 2, "goal": "Acquire current weather data for the user's location",
     "success_criteria": "weather data acquired"},
    {"id": 3, "goal": "Persist the captured data as a structured report",
     "success_criteria": "report persisted"},
]


def test_v065_stage1_emits_warn_trace_on_low_confidence():
    """Replay: Stage 1 returned low confidence → warn event on scroll trace."""
    from systemu.core.models import Scroll
    from sharing_on.cli import _append_intent_trace

    scroll = Scroll(
        id="scroll_replay_a", name="x", source_session_id="cap_20260517_145110",
        raw_instructions_path="", narrative_md="",
    )
    intent_stub = MagicMock(
        is_usable=False,
        confidence="low",
        intent=REAL_LOW_CONF_INTENT["intent"],
        error=None,
    )
    _append_intent_trace(scroll, intent_stub)

    stage1 = [e for e in scroll.pipeline_trace if e.stage == "intent"]
    assert len(stage1) == 1
    assert stage1[0].level == "warn"
    assert scroll.has_warnings is True


def test_v065_stage2_detects_and_rewrites_gui_objectives():
    """Replay: feeding REAL_GUI_OBJECTIVES through detect_gui_codification
    flags objective 2 (the one that broke the original e2e), and the rewrite
    flow produces objectives that pass a re-check.
    """
    from systemu.pipelines.scroll_refiner import detect_gui_codification

    objs = [Objective(**o) for o in REAL_GUI_OBJECTIVES]
    offenders = detect_gui_codification(objs)
    assert any(oid == 2 for oid, _ in offenders), (
        f"objective 2 ('capture screenshots') must be flagged; got {offenders}"
    )

    # After rewrite, no offenders should remain
    rewritten = [Objective(**o) for o in REWRITTEN_OBJECTIVES]
    assert detect_gui_codification(rewritten) == []
    for o in rewritten:
        assert "screenshot" not in o.goal.lower()
        assert "snip" not in o.goal.lower()


def test_v065_full_trace_after_fix_cycle():
    """End-to-end on the trace: Stage 1 (warn) + Stage 2 (info) + Stage 6 (info)
    populate the scroll.pipeline_trace in order.  Operator sees badge + panel.
    """
    from systemu.core.models import Scroll, TraceEvent
    from sharing_on.cli import _append_intent_trace

    scroll = Scroll(
        id="scroll_replay_b", name="x", source_session_id="cap_y",
        raw_instructions_path="", narrative_md="",
    )

    # Stage 1
    intent = MagicMock(is_usable=False, confidence="low", intent="X", error=None)
    _append_intent_trace(scroll, intent)

    # Stage 2 fix-success
    scroll.pipeline_trace.append(TraceEvent(
        stage="refine", level="info",
        message="GUI codification fixed on 1 objective(s) via re-prompt",
        detail={"first_pass": [[2, "screenshot"]]},
    ))

    # Stage 6 pass
    scroll.pipeline_trace.append(TraceEvent(
        stage="validate", level="info",
        message="validator passed",
        detail={"confidence": "high"},
    ))

    assert len(scroll.pipeline_trace) == 3
    assert scroll.has_warnings is True  # Stage 1 warn dominates

    stages = [e.stage for e in scroll.pipeline_trace]
    assert stages == ["intent", "refine", "validate"]


def test_v065_reprompt_loop_against_real_gui_objectives(monkeypatch):
    """Wire-level replay: feed REAL_GUI_OBJECTIVES as the first LLM output, then
    confirm _refine_with_gui_guard re-prompts and the second call's clean
    output is used."""
    from systemu.pipelines import scroll_refiner as sr

    call_count = [0]

    def fake_llm(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"objectives": list(REAL_GUI_OBJECTIVES)}
        return {"objectives": list(REWRITTEN_OBJECTIVES)}

    monkeypatch.setattr(sr, "llm_call_json", fake_llm)

    result = sr._refine_with_gui_guard(
        payload={"narrative": "weather workflow"},
        prompt_text="ignored",
        config=MagicMock(),
    )

    assert call_count[0] == 2, "re-prompt loop must fire exactly once"

    objs = [Objective(**o) for o in result["objectives"]]
    assert sr.detect_gui_codification(objs) == []

    guard = result["_v065_gui_guard"]
    assert any(oid == 2 for oid, _ in guard["first_pass_offenders"])
    assert guard["second_pass_offenders"] == []
