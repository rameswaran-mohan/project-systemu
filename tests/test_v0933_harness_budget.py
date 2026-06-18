"""C — iteration-budget surfacing in the Shadow execute-step user payload.

These tests pin the v0.9.33 fix: the per-iteration LLM user payload must
expose iteration / iter_budget / iterations_remaining so a budget-aware
decision is reachable, plus an escalating low-budget nudge.

The helper is a PURE function (no I/O, no input mutation) so it is unit-tested
directly. A separate ``inspect.getsource`` seam test guards the live wiring in
``ShadowRuntime.execute`` — the recurring failure mode in this codebase is a
green helper that the loop silently stops calling (or that drops the downstream
operator_hint / loop_guard mutation blocks).
"""
import importlib
import inspect
import pathlib


def _helper():
    mod = importlib.import_module("systemu.runtime.shadow_runtime")
    assert hasattr(mod, "_build_user_payload"), (
        "expected module-level helper _build_user_payload in shadow_runtime"
    )
    return mod._build_user_payload


def _base_kwargs(**over):
    """Minimal kwargs for a single-objective, objective-mode payload."""
    kw = dict(
        shadow_name="FinanceTracker",
        output_dir="/out",
        current_date="2026-06-17",
        current_datetime_utc="2026-06-17T00:00:00Z",
        use_objectives=True,
        intent="do the thing",
        scroll_json=[{"id": 1, "goal": "g"}],
        completed_objectives={1},
        pending_objectives=[{"id": 2, "goal": "g2"}],
        current_ab=1,
        available_tools=[{"id": "t1", "name": "web_screenshot"}],
        history=[],
        last_snapshot=None,
        iteration=3,
        iter_budget=30,
    )
    kw.update(over)
    return kw


# ─────────────────────────────────────────────────────────────────────────────
# C.1 — budget fields + objective/action-block parity
# ─────────────────────────────────────────────────────────────────────────────

def test_payload_contains_iteration_budget_fields():
    build = _helper()
    payload = build(**_base_kwargs(iteration=3, iter_budget=30))
    assert payload["iteration"] == 3
    assert payload["iter_budget"] == 30
    assert payload["iterations_remaining"] == 27  # iter_budget - iteration


def test_payload_preserves_objective_mode_keys():
    build = _helper()
    payload = build(**_base_kwargs())
    # Pre-existing keys must still be present and unchanged in shape.
    assert payload["shadow_name"] == "FinanceTracker"
    assert payload["output_dir"] == "/out"
    assert payload["intent"] == "do the thing"
    assert payload["objectives"] == [{"id": 1, "goal": "g"}]
    assert payload["completed_objectives"] == [1]
    assert payload["pending_objectives"] == [{"id": 2, "goal": "g2"}]
    assert payload["available_tools"] == [{"id": "t1", "name": "web_screenshot"}]
    assert "history" in payload
    assert "last_snapshot" in payload
    # Action-block-only keys must NOT leak into objective mode.
    assert "current_action_block" not in payload
    assert "pending_action_blocks" not in payload


def test_payload_action_block_mode_branch():
    build = _helper()
    payload = build(**_base_kwargs(
        use_objectives=False,
        scroll_json=[{"step_number": 1}, {"step_number": 2}],
        current_ab=2,
    ))
    assert payload["current_action_block"] == 2
    assert payload["pending_action_blocks"] == [{"step_number": 2}]
    # Objective-mode keys must NOT leak into action-block mode.
    assert "objectives" not in payload
    assert "pending_objectives" not in payload
    # Budget fields are present in BOTH modes.
    assert payload["iteration"] == 3
    assert payload["iter_budget"] == 30
    assert payload["iterations_remaining"] == 27


def test_payload_is_a_fresh_mutable_dict():
    """The loop mutates the returned dict (v2-aug, operator_hint, loop_guard).

    The helper must hand back a plain dict whose ``available_tools`` is a
    mutable list it does not alias from the caller's input, so the downstream
    in-loop mutation blocks are safe.
    """
    build = _helper()
    tools_in = [{"id": "t1", "name": "web_screenshot"}]
    payload = build(**_base_kwargs(available_tools=tools_in))
    # Caller can add keys (operator_hint / loop_guard_notice) without error.
    payload["operator_hint"] = "x"
    payload["loop_guard_notice"] = "y"
    payload["available_tools"].append({"id": "t2", "name": "extra"})
    # Budget fields survive alongside the in-loop additions.
    assert payload["iter_budget"] == 30
    assert payload["operator_hint"] == "x"
    assert payload["loop_guard_notice"] == "y"


# ─────────────────────────────────────────────────────────────────────────────
# C.2 — low-budget nudge (escalating: fires each low-budget iteration while
# work remains; NOT a single one-shot — re-reminding as the budget dwindles).
# ─────────────────────────────────────────────────────────────────────────────

def test_low_budget_notice_fires_when_remaining_low_and_pending():
    build = _helper()
    # remaining = 30 - 28 = 2 (<=3) and objectives pending -> notice present
    payload = build(**_base_kwargs(
        iteration=28, iter_budget=30,
        pending_objectives=[{"id": 2, "goal": "g2"}],
    ))
    assert payload["iterations_remaining"] == 2
    assert "low_budget_notice" in payload
    assert "2 iteration" in payload["low_budget_notice"]


def test_no_notice_when_budget_ample():
    build = _helper()
    # remaining = 30 - 3 = 27 (>3) -> no notice even with pending work
    payload = build(**_base_kwargs(
        iteration=3, iter_budget=30,
        pending_objectives=[{"id": 2, "goal": "g2"}],
    ))
    assert payload["iterations_remaining"] == 27
    assert "low_budget_notice" not in payload


def test_no_notice_when_low_but_no_pending_objectives():
    build = _helper()
    # remaining low BUT nothing pending -> no notice (don't nag a finished run)
    payload = build(**_base_kwargs(
        iteration=29, iter_budget=30,
        completed_objectives={1, 2},
        pending_objectives=[],
    ))
    assert payload["iterations_remaining"] == 1
    assert "low_budget_notice" not in payload


def test_no_notice_when_remaining_zero_or_negative():
    build = _helper()
    # at/over budget -> guard `0 < remaining` prevents a notice
    payload = build(**_base_kwargs(
        iteration=30, iter_budget=30,
        pending_objectives=[{"id": 2, "goal": "g2"}],
    ))
    assert payload["iterations_remaining"] == 0
    assert "low_budget_notice" not in payload


def test_notice_escalates_each_low_iteration():
    """Escalating (not one-shot): fires on every low-budget iteration while
    work remains, with the live remaining count each time."""
    build = _helper()
    p3 = build(**_base_kwargs(iteration=27, iter_budget=30))  # remaining 3
    p2 = build(**_base_kwargs(iteration=28, iter_budget=30))  # remaining 2
    p1 = build(**_base_kwargs(iteration=29, iter_budget=30))  # remaining 1
    assert "3 iteration" in p3["low_budget_notice"]
    assert "2 iteration" in p2["low_budget_notice"]
    assert "1 iteration" in p1["low_budget_notice"]


def test_notice_respects_dynamic_extended_budget():
    """iterations_remaining tracks the LIVE (possibly COMPUTE-extended)
    iter_budget, not a fixed MAX_ITERATIONS."""
    build = _helper()
    # Budget extended to 40; at iteration 35 -> remaining 5 -> ample, no notice.
    payload = build(**_base_kwargs(iteration=35, iter_budget=40))
    assert payload["iterations_remaining"] == 5
    assert "low_budget_notice" not in payload


# ─────────────────────────────────────────────────────────────────────────────
# Real-seam guard — the live loop must keep calling the helper with the LIVE
# budget AND keep the downstream mutation blocks (defense against silent
# "green helper, dead production" regressions).
# ─────────────────────────────────────────────────────────────────────────────

def test_execute_wires_helper_with_live_budget_and_keeps_mutations():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    # The loop builds the payload via the helper, named (so the mutation
    # blocks can augment it), using the LIVE/dynamic budget.
    assert "_user_payload = _build_user_payload(" in src, (
        "execute() must assign _user_payload from the helper"
    )
    assert "iter_budget=_iter_budget" in src, (
        "execute() must pass the LIVE _iter_budget (dynamic, COMPUTE-extendable), "
        "not MAX_ITERATIONS"
    )
    # The 3 downstream mutation blocks must survive the refactor.
    assert '_user_payload["operator_hint"]' in src
    assert '_user_payload["loop_guard_notice"]' in src
    assert "_llm_user = json.dumps(_user_payload)" in src


# ─────────────────────────────────────────────────────────────────────────────
# C.3 — prompt documents the budget
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_text():
    p = (
        pathlib.Path(__file__).resolve().parents[1]
        / "systemu" / "prompts" / "execute_step.md"
    )
    return p.read_text(encoding="utf-8")


def test_prompt_advertises_iteration_budget_fields():
    text = _prompt_text()
    # The "you will receive" list must mention the budget.
    assert "iter_budget" in text
    assert "iterations_remaining" in text


def test_prompt_has_budget_decision_rule():
    text = _prompt_text()
    lowered = text.lower()
    # A Decision Rule must instruct budget-aware winding-down.
    assert "iterations_remaining" in text
    assert ("budget" in lowered) and ("wind down" in lowered or "consolidate" in lowered)
