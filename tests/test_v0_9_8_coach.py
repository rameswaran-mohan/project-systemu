"""v0.9.8 Phase 2 — autonomous mid-run steering coach.

When a run stalls (no objective credit for N iterations), the runtime FIRST tries
to self-steer via ``coach.generate_steer`` (an LLM produces ONE corrective
instruction injected as a hint to retry) and only escalates to a human operator
after ``auto_coach_max_steers`` self-steers fail.

These tests stub the LLM client entirely — NO network calls are made.

Coverage:
  - generate_steer → "" when the LLM client raises (fail-safe → operator escalates).
  - generate_steer → "" when the steer's confidence < 0.5.
  - generate_steer → "" when the LLM returns an empty steer.
  - generate_steer → the steer string when the coach is confident.
  - Source guard on ShadowRuntime.execute: the stuck block references
    generate_steer / coach, auto_coach_max_steers and _coach_steers_used, and the
    steer path sets _operator_hint then continues (retry, not escalate).
"""
from __future__ import annotations

import inspect

from systemu.runtime import coach
from systemu.runtime.coach import generate_steer


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

class _Cfg:
    """Bare config stub — coach reads only verifier_tier off it; the LLM call
    itself is always monkeypatched so nothing else matters."""
    verifier_tier = 1


class _Objective:
    def __init__(self, id="1", goal="determine the user's city from their IP"):
        self.id = id
        self.goal = goal


_HISTORY = [
    {"role": "tool_call", "tool": "fetch_web", "params": {"url": "http://ip-api.com"}},
    {"role": "tool_result", "result": {"error": "403 Forbidden"}},
    {"role": "thought", "thought": "the page keeps 403'ing"},
]


# ─────────────────────────────────────────────────────────────────────────────
#  1. generate_steer — fail-safe to "" on LLM error
# ─────────────────────────────────────────────────────────────────────────────

class TestSteerFallback:
    def test_llm_raises_returns_empty(self, monkeypatch):
        """When the LLM client raises, generate_steer returns "" and never raises."""
        def _boom(*args, **kwargs):
            raise RuntimeError("LLM unavailable")

        monkeypatch.setattr(coach, "llm_call_json", _boom)

        out = generate_steer(
            objective=_Objective(),
            reason="no objective credit for 5 iterations",
            tools_tried=["fetch_web"],
            history=_HISTORY,
            config=_Cfg(),
        )
        assert out == ""

    def test_malformed_output_returns_empty(self, monkeypatch):
        """A response without 'steer' → ""."""
        monkeypatch.setattr(
            coach, "llm_call_json",
            lambda *a, **k: {"garbage": "no steer key"},
        )
        out = generate_steer(
            objective=_Objective(), reason="stalled",
            tools_tried=[], history=_HISTORY, config=_Cfg(),
        )
        assert out == ""

    def test_empty_steer_returns_empty(self, monkeypatch):
        """A confident response whose steer text is empty → ""."""
        monkeypatch.setattr(
            coach, "llm_call_json",
            lambda *a, **k: {"steer": "   ", "confidence": 0.99},
        )
        out = generate_steer(
            objective=_Objective(), reason="stalled",
            tools_tried=[], history=_HISTORY, config=_Cfg(),
        )
        assert out == ""


# ─────────────────────────────────────────────────────────────────────────────
#  2. generate_steer — low-confidence steer is discarded
# ─────────────────────────────────────────────────────────────────────────────

class TestSteerLowConfidence:
    def test_below_threshold_returns_empty(self, monkeypatch):
        """confidence < 0.5 → "" (caller escalates to operator)."""
        monkeypatch.setattr(
            coach, "llm_call_json",
            lambda *a, **k: {"steer": "try something else", "confidence": 0.4},
        )
        out = generate_steer(
            objective=_Objective(), reason="stalled",
            tools_tried=["fetch_web"], history=_HISTORY, config=_Cfg(),
        )
        assert out == ""

    def test_exactly_at_threshold_returns_steer(self, monkeypatch):
        """confidence == 0.5 is the inclusive floor → the steer is returned."""
        monkeypatch.setattr(
            coach, "llm_call_json",
            lambda *a, **k: {"steer": "use find_places for 'near me' lookups",
                             "confidence": 0.5},
        )
        out = generate_steer(
            objective=_Objective(), reason="stalled",
            tools_tried=["fetch_web"], history=_HISTORY, config=_Cfg(),
        )
        assert out == "use find_places for 'near me' lookups"


# ─────────────────────────────────────────────────────────────────────────────
#  3. generate_steer — confident steer is returned verbatim
# ─────────────────────────────────────────────────────────────────────────────

class TestSteerConfident:
    def test_confident_returns_steer_string(self, monkeypatch):
        """A confident steer is returned (stripped) for the caller to inject."""
        monkeypatch.setattr(
            coach, "llm_call_json",
            lambda *a, **k: {
                "steer": "The page 403'd; use search_web for an alternative source.",
                "confidence": 0.9,
            },
        )
        out = generate_steer(
            objective=_Objective(),
            reason="no objective credit for 5 iterations",
            tools_tried=["fetch_web"],
            history=_HISTORY,
            config=_Cfg(),
        )
        assert out == "The page 403'd; use search_web for an alternative source."

    def test_payload_carries_objective_and_reason(self, monkeypatch):
        """The user payload sent to the LLM includes the objective + reason so the
        coach can diagnose the stall."""
        captured = {}

        def _capture(*args, **kwargs):
            captured["user"] = kwargs.get("user", "")
            return {"steer": "write the result to the output file now", "confidence": 0.8}

        monkeypatch.setattr(coach, "llm_call_json", _capture)
        out = generate_steer(
            objective=_Objective(id="2", goal="write the city to a file"),
            reason="no objective credit for 5 iterations",
            tools_tried=["fetch_web"],
            history=_HISTORY,
            config=_Cfg(),
        )
        assert out == "write the result to the output file now"
        assert "write the city to a file" in captured["user"]
        assert "no objective credit" in captured["user"]


# ─────────────────────────────────────────────────────────────────────────────
#  4. Source guard — coach wired into ShadowRuntime.execute's stuck block
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteWiring:
    def test_execute_references_coach_and_counter(self):
        """The stuck block must call into the coach, gate on auto_coach_max_steers,
        and track _coach_steers_used."""
        from systemu.runtime.shadow_runtime import ShadowRuntime
        src = inspect.getsource(ShadowRuntime.execute)
        assert "generate_steer" in src
        assert "coach" in src  # `from systemu.runtime.coach import generate_steer`
        assert "auto_coach_max_steers" in src
        assert "_coach_steers_used" in src
        assert "auto_coach_enabled" in src

    def test_steer_path_sets_hint_then_continues(self):
        """On a usable steer the runtime sets _operator_hint and `continue`s the
        loop (retry with the steer) BEFORE the operator-escalation call."""
        from systemu.runtime.shadow_runtime import ShadowRuntime
        src = inspect.getsource(ShadowRuntime.execute)

        # The steer block must precede the operator escalation (_ask_stuck_or_degrade).
        steer_idx = src.index("generate_steer")
        escalate_idx = src.index("_ask_stuck_or_degrade")
        assert steer_idx < escalate_idx, "coach must run BEFORE operator escalation"

        # Within the steer block: hint is set, counter bumped, then continue.
        block = src[steer_idx:escalate_idx]
        assert "self._operator_hint" in block
        assert "self._coach_steers_used += 1" in block
        assert "self._iters_since_obj_credit = 0" in block
        assert "self._same_tool_fail_streak.clear()" in block
        assert "continue" in block

    def test_counter_reset_at_run_start(self):
        """_coach_steers_used must be reset to 0 per run alongside the other
        stuck-guard counters."""
        from systemu.runtime.shadow_runtime import ShadowRuntime
        src = inspect.getsource(ShadowRuntime.execute)
        assert "self._coach_steers_used = 0" in src
