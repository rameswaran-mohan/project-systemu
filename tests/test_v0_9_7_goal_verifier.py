"""Tests for systemu.runtime.goal_verifier — Phase 1.4a.

All LLM calls are monkeypatched; no network access occurs.

Coverage:
- goal + delta WITH file → verified True
- goal + EMPTY delta     → verified False (anti-hallucination gate)
- informational goal + chat_result + empty delta → verified True
- LLM raises            → verified False, no crash
- disabled via config   → passthrough True (no LLM call)
- raw goal string (not prior_criteria) is sent to the LLM
"""
import json
import pytest
from sharing_on.config import Config
from systemu.runtime.state_delta import StateDelta
from systemu.runtime import goal_verifier


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cfg(**overrides):
    cfg = Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _empty_delta() -> StateDelta:
    return StateDelta(
        files_added=[],
        files_modified=[],
        audit_entries_added=[],
        chat_result_set=None,
        vault_records_added=[],
        iteration_start_ts="2026-06-08T00:00:00Z",
        extensions={},
    )


def _delta_with_file(path: str = "/tmp/city.txt") -> StateDelta:
    return StateDelta(
        files_added=[{"path": path, "size": 42, "preview": "London"}],
        files_modified=[],
        audit_entries_added=[],
        chat_result_set=None,
        vault_records_added=[],
        iteration_start_ts="2026-06-08T00:00:00Z",
        extensions={},
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestGoalVerifierFileGoal:
    """Goal that requires a durable file artifact."""

    def test_goal_with_file_in_delta_returns_verified_true(self, monkeypatch):
        """A goal needing a file, and the delta contains that file → verified True."""
        def fake_llm(*, tier, system, user, config, **kw):
            return {
                "verified": True,
                "reason": "city.txt was written with the city name",
                "derived_criteria": ["A file containing the city name must exist"],
            }

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        result = goal_verifier.verify_goal(
            goal="find my city from IP and write it to /tmp/city.txt",
            delta=_delta_with_file("/tmp/city.txt"),
            config=_cfg(),
        )

        assert result["verified"] is True
        assert isinstance(result["reason"], str) and result["reason"]
        assert isinstance(result["derived_criteria"], list)
        assert len(result["derived_criteria"]) >= 1

    def test_goal_with_empty_delta_returns_verified_false(self, monkeypatch):
        """Anti-hallucination: a goal needing a file but delta is empty → verified False."""
        def fake_llm(*, tier, system, user, config, **kw):
            return {
                "verified": False,
                "reason": "no file was written; delta shows no durable artifact",
                "derived_criteria": ["A file at /tmp/city.txt must exist with the city name"],
            }

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        result = goal_verifier.verify_goal(
            goal="find my city from IP and write it to /tmp/city.txt",
            delta=_empty_delta(),
            config=_cfg(),
        )

        assert result["verified"] is False
        assert "artifact" in result["reason"].lower() or "file" in result["reason"].lower()


class TestGoalVerifierInformationalGoal:
    """Goal that is purely informational — no file artifact required."""

    def test_informational_goal_satisfied_by_chat_result(self, monkeypatch):
        """An informational goal with a substantive chat_result → verified True
        even when the file delta is empty."""
        def fake_llm(*, tier, system, user, config, **kw):
            return {
                "verified": True,
                "reason": "chat reply directly answers the question",
                "derived_criteria": [
                    "The chat reply must contain a direct answer to the weather question"
                ],
            }

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        result = goal_verifier.verify_goal(
            goal="What is the weather like in Tokyo today?",
            delta=_empty_delta(),
            config=_cfg(),
            chat_result="Tokyo is currently 22°C and partly cloudy.",
        )

        assert result["verified"] is True
        assert result["derived_criteria"]


class TestGoalVerifierErrorHandling:
    """Fail-safe: LLM errors must never produce a false pass."""

    def test_llm_exception_returns_verified_false_no_crash(self, monkeypatch):
        """If the LLM call raises for any reason, return verified=False (not a crash,
        not a false True)."""
        def fake_llm(**kw):
            raise RuntimeError("network timeout")

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        result = goal_verifier.verify_goal(
            goal="write a report to /tmp/report.txt",
            delta=_empty_delta(),
            config=_cfg(),
        )

        assert result["verified"] is False
        assert "verifier error" in result["reason"]
        assert result["derived_criteria"] == []

    def test_malformed_llm_response_returns_verified_false(self, monkeypatch):
        """If the LLM returns JSON without a 'verified' key, return False."""
        def fake_llm(**kw):
            return {"something_else": "unexpected"}

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        result = goal_verifier.verify_goal(
            goal="write a report to /tmp/report.txt",
            delta=_empty_delta(),
            config=_cfg(),
        )

        assert result["verified"] is False
        assert "malformed" in result["reason"] or "unparsable" in result["reason"]


class TestGoalVerifierConfigGate:
    """Config gate: when disabled, passthrough True without calling the LLM."""

    def test_disabled_via_config_attribute_returns_true_no_llm_call(self, monkeypatch):
        """When config.goal_verifier_enabled is False, verify_goal() returns True
        immediately without calling the LLM (old per-objective path still guards)."""
        called = []

        def fake_llm(**kw):
            called.append(1)
            return {"verified": False, "reason": "should not be called"}

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        cfg = _cfg(goal_verifier_enabled=False)
        result = goal_verifier.verify_goal(
            goal="find my city from IP and write it to /tmp/city.txt",
            delta=_empty_delta(),
            config=cfg,
        )

        assert result["verified"] is True
        assert called == []  # LLM was never invoked

    def test_disabled_via_env_var_returns_true_no_llm_call(self, monkeypatch):
        """SYSTEMU_GOAL_VERIFIER_ENABLED=false disables the verifier."""
        called = []

        def fake_llm(**kw):
            called.append(1)
            return {"verified": False, "reason": "should not be called"}

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)
        monkeypatch.setenv("SYSTEMU_GOAL_VERIFIER_ENABLED", "false")

        # Config without the attribute so env-var resolution path is exercised
        cfg = Config()
        if hasattr(cfg, "goal_verifier_enabled"):
            # Remove attribute so env-var path is exercised
            del cfg.__dict__["goal_verifier_enabled"]

        result = goal_verifier.verify_goal(
            goal="write a file",
            delta=_empty_delta(),
            config=cfg,
        )

        assert result["verified"] is True
        assert called == []


class TestGoalVerifierRawGoalContract:
    """The raw goal string — not prior_criteria — must be the authoritative bar
    sent to the LLM."""

    def test_raw_goal_string_is_in_llm_user_payload(self, monkeypatch):
        """Assert that the user payload sent to the LLM contains the verbatim
        goal string, not just prior_criteria."""
        captured_user = {}

        def fake_llm(*, tier, system, user, config, **kw):
            captured_user["payload"] = json.loads(user)
            return {
                "verified": True,
                "reason": "ok",
                "derived_criteria": ["goal met"],
            }

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        raw_goal = "find my city from IP and write it to /tmp/city.txt"
        misleading_prior = ["just check if a global var is set"]

        goal_verifier.verify_goal(
            goal=raw_goal,
            delta=_delta_with_file(),
            config=_cfg(),
            prior_criteria=misleading_prior,
        )

        payload = captured_user["payload"]
        # The raw goal must appear verbatim in the payload
        assert payload["goal"] == raw_goal
        # prior_criteria are present only as hints
        assert payload["prior_criteria"] == misleading_prior
        # Confirm the authoritative key is "goal", not something derived
        assert "goal" in payload

    def test_prior_criteria_absence_does_not_break_call(self, monkeypatch):
        """verify_goal must work when prior_criteria=None (the common case)."""
        def fake_llm(*, tier, system, user, config, **kw):
            payload = json.loads(user)
            assert payload["prior_criteria"] == []
            return {
                "verified": True,
                "reason": "ok",
                "derived_criteria": ["criterion"],
            }

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        result = goal_verifier.verify_goal(
            goal="write output to /tmp/out.txt",
            delta=_delta_with_file("/tmp/out.txt"),
            config=_cfg(),
            prior_criteria=None,
        )

        assert result["verified"] is True


class TestGoalVerifierReturnContract:
    """Verify the return dict always has the three required keys with correct types."""

    @pytest.mark.parametrize("llm_verified", [True, False])
    def test_return_dict_always_has_all_three_keys(self, monkeypatch, llm_verified):
        def fake_llm(**kw):
            return {
                "verified": llm_verified,
                "reason": "test reason",
                "derived_criteria": ["c1", "c2"],
            }

        monkeypatch.setattr(goal_verifier, "llm_call_json", fake_llm)

        result = goal_verifier.verify_goal(
            goal="some goal",
            delta=_empty_delta(),
            config=_cfg(),
        )

        assert "verified" in result
        assert "reason" in result
        assert "derived_criteria" in result
        assert isinstance(result["verified"], bool)
        assert isinstance(result["reason"], str)
        assert isinstance(result["derived_criteria"], list)

    def test_error_path_still_returns_all_three_keys(self, monkeypatch):
        monkeypatch.setattr(goal_verifier, "llm_call_json",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

        result = goal_verifier.verify_goal(
            goal="some goal",
            delta=_empty_delta(),
            config=_cfg(),
        )

        assert "verified" in result
        assert "reason" in result
        assert "derived_criteria" in result
        assert result["verified"] is False
