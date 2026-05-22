"""Tests for v0.6.0-d.5 — Stage 3.5 skill intent contracts + validator + recalibration.

Covers:
  * Skill model: new fields default sensibly + round-trip through JSON vault
  * SkillValidator: empty target_outcomes → missing_contract blocker
  * SkillValidator: parses LLM blockers (gui_codification, outcome_mismatch, etc.)
  * SkillValidator: is_enabled honours SYSTEMU_SKILL_VALIDATOR + SYSTEMU_SCROLL_VALIDATOR
  * Recalibrator: re-author bump_skill + fork_new_skill flows
  * Recalibrator: is_low_risk_skill_recalibration (6 conservative criteria)
  * Recalibrator: decay_effectiveness crosses RECAL_THRESHOLD correctly
  * RECALIBRATE_SKILL is in execution_mind ACTION_VOCABULARY + HIGH_IMPACT_ACTIONS
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder", "notifications"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    (tmp_path / "global_memory.jsonl").write_text("")
    (tmp_path / "chat_history.jsonl").write_text("")
    return Vault(str(tmp_path))


def _config():
    c = MagicMock()
    c.intelligent_supervisor_enabled = True
    c.openrouter_api_key = "test"
    c.tier1_model = "t"
    c.tier2_model = "t2"
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Skill model: new fields

class TestSkillModelFields:
    def test_pydantic_defaults(self):
        from systemu.core.models import Skill
        s = Skill(id="s1", name="x", description="y")
        assert s.target_outcomes == []
        assert s.produces == []
        assert s.effectiveness_score == 1.0
        assert s.skill_version == 1
        assert s.evolution_history == []

    def test_pydantic_accepts_values(self):
        from systemu.core.models import Skill
        s = Skill(
            id="s2", name="x", description="y",
            target_outcomes=["doc data"],
            produces=["data"],
            effectiveness_score=0.7,
            skill_version=3,
            evolution_history=[{"version": 2, "reason": "x"}],
        )
        assert s.target_outcomes == ["doc data"]
        assert s.effectiveness_score == 0.7

    def test_json_vault_round_trip(self, vault):
        from systemu.core.models import Skill
        original = Skill(
            id="skill_rt", name="weather_capture",
            description="Capture weather",
            target_outcomes=["document weather data"],
            produces=["data", "structured_document"],
            effectiveness_score=0.6,
            skill_version=2,
            evolution_history=[{"version": 1, "reason": "init"}],
        )
        vault.save_skill(original)
        loaded = vault.get_skill("skill_rt")
        assert loaded.target_outcomes == ["document weather data"]
        assert loaded.produces == ["data", "structured_document"]
        assert loaded.effectiveness_score == 0.6
        assert loaded.skill_version == 2

    def test_skill_md_does_not_contain_new_fields(self, vault, tmp_path):
        """v0.6.0-d.5 §8 hard constraint: SKILL.md frontmatter stays the
        Anthropic standard 5 keys.  New fields live only in JSON+DB."""
        from systemu.core.models import Skill
        s = Skill(
            id="skill_compliance", name="weather_capture",
            description="Capture weather",
            target_outcomes=["document data"],
            produces=["data"],
            effectiveness_score=0.5,
            skill_version=2,
        )
        vault.save_skill(s)

        md_path = next((tmp_path / "skills").rglob("SKILL.md"), None)
        assert md_path is not None
        body = md_path.read_text(encoding="utf-8")
        # Standard 5 keys present
        assert "name: weather_capture" in body
        assert "description:" in body
        assert "category:" in body
        assert "proficiency_level:" in body
        # New fields ABSENT from the portable export
        assert "target_outcomes" not in body
        assert "produces:" not in body
        assert "effectiveness_score" not in body
        assert "skill_version" not in body


# ─────────────────────────────────────────────────────────────────────────────
# Skill validator

class TestSkillValidator:
    def test_disabled_returns_valid_low_confidence(self, vault, monkeypatch):
        monkeypatch.delenv("SYSTEMU_SKILL_VALIDATOR", raising=False)
        monkeypatch.delenv("SYSTEMU_SCROLL_VALIDATOR", raising=False)

        from systemu.core.models import Skill
        from systemu.pipelines import skill_validator as sv

        cfg = MagicMock(); cfg.intelligent_supervisor_enabled = False
        result = sv.validate_skill(
            Skill(id="s", name="x", description="y",
                  target_outcomes=["a"], produces=["data"]),
            config=cfg, vault=vault,
        )
        assert result.valid is True
        assert result.confidence == "low"

    def test_empty_target_outcomes_short_circuits_missing_contract(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SKILL_VALIDATOR", "1")
        from systemu.core.models import Skill
        from systemu.pipelines import skill_validator as sv

        result = sv.validate_skill(
            Skill(id="s", name="x", description="y",
                  target_outcomes=[], produces=["data"]),
            config=_config(), vault=vault,
        )
        assert result.valid is False
        assert result.blockers[0].category == "missing_contract"

    def test_empty_produces_short_circuits_missing_contract(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SKILL_VALIDATOR", "1")
        from systemu.core.models import Skill
        from systemu.pipelines import skill_validator as sv

        result = sv.validate_skill(
            Skill(id="s", name="x", description="y",
                  target_outcomes=["a"], produces=[]),
            config=_config(), vault=vault,
        )
        assert result.valid is False
        assert result.blockers[0].category == "missing_contract"

    def test_parses_llm_blockers(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SKILL_VALIDATOR", "1")
        from systemu.core.models import Skill
        from systemu.pipelines import skill_validator as sv

        def fake_llm(**kw):
            return {
                "valid": False, "confidence": "high",
                "blockers": [{
                    "category": "gui_codification",
                    "explanation": "instructions enumerate Snipping Tool clicks",
                    "suggested_fix": "rewrite as outcome-described steps",
                }],
                "summary": "gui-codifying skill blocked",
            }

        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)

        result = sv.validate_skill(
            Skill(id="s", name="weather_capture", description="...",
                  target_outcomes=["doc data"], produces=["image"],
                  instructions_md="Open Snipping Tool, drag to select..."),
            config=_config(), vault=vault,
        )
        assert result.valid is False
        assert result.blockers[0].category == "gui_codification"

    def test_llm_failure_fails_open(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SKILL_VALIDATOR", "1")
        from systemu.core.models import Skill
        from systemu.pipelines import skill_validator as sv

        def boom(**kw):
            raise RuntimeError("network down")
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", boom)

        result = sv.validate_skill(
            Skill(id="s", name="x", description="y",
                  target_outcomes=["a"], produces=["data"]),
            config=_config(), vault=vault,
        )
        assert result.valid is True   # fail-open
        assert result.error is not None


# ─────────────────────────────────────────────────────────────────────────────
# Skill recalibrator

class TestSkillRecalibrator:
    def _skill(self):
        from systemu.core.models import Skill
        return Skill(
            id="skill_recal", name="weather_capture",
            description="Capture weather",
            target_outcomes=["document weather data"],
            produces=["data"],
            instructions_md="Old body — uses screenshot wrongly",
            required_tool_names=["web_screenshot"],
            skill_version=2,
            effectiveness_score=0.3,
        )

    def test_recalibrate_returns_new_body(self, vault, monkeypatch):
        from systemu.pipelines import skill_recalibrator as sr

        def fake_llm(**kw):
            return {
                "new_instructions_md": "1. Use fetch_json to get weather data. 2. Format the JSON as a markdown report. 3. Use write_markdown_file to persist.",
                "tool_selection_changed": True,
                "new_required_tool_names": ["fetch_json", "write_markdown_file"],
                "rationale": "Replaces screenshot path with API + markdown for data intent",
                "confidence": "high",
                "destructive_risk": "none",
                "side_effects_introduced": [],
            }
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)

        result = sr.recalibrate_skill(
            self._skill(),
            failure_context={
                "execution_id": "exec_x",
                "status": "partial",
                "summary": "Reached max iterations",
                "recent_failure_observations": [],
                "objective_in_flight": "fetch weather data",
            },
            config=_config(), vault=vault,
            mode="bump_skill",
        )

        assert result.success is True
        assert "fetch_json" in result.new_instructions_md
        assert "fetch_json" in result.new_required_tools
        assert result.confidence == "high"

    def test_invalid_mode_rejected(self, vault):
        from systemu.pipelines import skill_recalibrator as sr
        result = sr.recalibrate_skill(
            self._skill(),
            failure_context={"status": "failed"},
            config=_config(), vault=vault,
            mode="bogus_mode",
        )
        assert result.success is False
        assert "invalid mode" in (result.error or "")

    def test_apply_bump_in_place(self, vault):
        from systemu.pipelines import skill_recalibrator as sr
        skill = self._skill()
        vault.save_skill(skill)

        result = sr.SkillRecalibrationResult(
            success=True, skill_id=skill.id, mode="bump_skill",
            new_instructions_md="New outcome-described body",
            new_required_tools=["fetch_json"],
            confidence="high",
            rationale="x",
        )
        updated = sr.apply_recalibration(skill, result, vault=vault, reason="test")

        # Same id, bumped version, fresh score, new body.
        reloaded = vault.get_skill(skill.id)
        assert reloaded.id == skill.id
        assert reloaded.skill_version == 3   # was 2
        assert reloaded.instructions_md == "New outcome-described body"
        assert reloaded.effectiveness_score == 1.0
        assert len(reloaded.evolution_history) == 1

    def test_apply_fork_creates_new_skill(self, vault):
        from systemu.pipelines import skill_recalibrator as sr
        skill = self._skill()
        vault.save_skill(skill)

        result = sr.SkillRecalibrationResult(
            success=True, skill_id=skill.id, mode="fork_new_skill",
            new_instructions_md="Forked outcome body",
            new_required_tools=["fetch_json"],
            confidence="high",
            rationale="x",
        )
        forked = sr.apply_recalibration(skill, result, vault=vault, reason="test")
        assert forked.id != skill.id
        assert "v3" in forked.name   # original was v2 → fork named with v3
        assert forked.instructions_md == "Forked outcome body"

        # Original unaffected.
        original = vault.get_skill(skill.id)
        assert original.instructions_md == "Old body — uses screenshot wrongly"


# ─────────────────────────────────────────────────────────────────────────────
# Low-risk classifier (auto-approve eligibility)

class TestLowRiskClassifier:
    def _result(self, **overrides):
        from systemu.pipelines.skill_recalibrator import SkillRecalibrationResult
        defaults = dict(
            success=True, skill_id="s", mode="fork_new_skill",
            confidence="high", destructive_risk="none",
            side_effects=[], new_instructions_md="x",
            new_required_tools=["fetch_json"],
        )
        defaults.update(overrides)
        return SkillRecalibrationResult(**defaults)

    def _skill(self, **overrides):
        from systemu.core.models import Skill
        base = dict(
            id="s", name="data_capture", description="x",
            target_outcomes=["a"], produces=["data"],
        )
        base.update(overrides)
        return Skill(**base)

    def test_happy_path_eligible(self):
        from systemu.pipelines.skill_recalibrator import is_low_risk_skill_recalibration
        ok, _ = is_low_risk_skill_recalibration(self._result(), self._skill())
        assert ok is True

    def test_failed_recal_blocked(self):
        from systemu.pipelines.skill_recalibrator import is_low_risk_skill_recalibration
        ok, reason = is_low_risk_skill_recalibration(
            self._result(success=False), self._skill(),
        )
        assert ok is False
        assert "did not succeed" in reason

    def test_bump_mode_always_blocked(self):
        from systemu.pipelines.skill_recalibrator import is_low_risk_skill_recalibration
        ok, reason = is_low_risk_skill_recalibration(
            self._result(mode="bump_skill"), self._skill(),
        )
        assert ok is False
        assert "bump_skill" in reason

    def test_low_confidence_blocked(self):
        from systemu.pipelines.skill_recalibrator import is_low_risk_skill_recalibration
        ok, _ = is_low_risk_skill_recalibration(
            self._result(confidence="medium"), self._skill(),
        )
        assert ok is False

    def test_destructive_risk_blocked(self):
        from systemu.pipelines.skill_recalibrator import is_low_risk_skill_recalibration
        ok, _ = is_low_risk_skill_recalibration(
            self._result(destructive_risk="medium"), self._skill(),
        )
        assert ok is False

    def test_side_effects_blocked(self):
        from systemu.pipelines.skill_recalibrator import is_low_risk_skill_recalibration
        ok, _ = is_low_risk_skill_recalibration(
            self._result(side_effects=["sends_email"]), self._skill(),
        )
        assert ok is False

    def test_destructive_skill_name_blocked(self):
        from systemu.pipelines.skill_recalibrator import is_low_risk_skill_recalibration
        ok, reason = is_low_risk_skill_recalibration(
            self._result(), self._skill(name="delete_user_data"),
        )
        assert ok is False
        assert "destructive" in reason

    def test_skill_with_side_effect_produces_blocked(self):
        from systemu.pipelines.skill_recalibrator import is_low_risk_skill_recalibration
        ok, reason = is_low_risk_skill_recalibration(
            self._result(), self._skill(produces=["side_effect"]),
        )
        assert ok is False
        assert "side_effect" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Effectiveness decay

class TestEffectivenessDecay:
    def test_failure_decays_by_02(self, vault):
        from systemu.core.models import Skill
        from systemu.pipelines.skill_recalibrator import decay_effectiveness

        s = Skill(id="s_decay", name="x", description="y",
                  target_outcomes=["a"], produces=["data"],
                  effectiveness_score=1.0)
        vault.save_skill(s)

        crossed = decay_effectiveness(s, status="failure", vault=vault)
        assert s.effectiveness_score == pytest.approx(0.8)
        assert crossed is False

    def test_partial_decays_by_05(self, vault):
        from systemu.core.models import Skill
        from systemu.pipelines.skill_recalibrator import decay_effectiveness

        s = Skill(id="s_decay", name="x", description="y",
                  target_outcomes=["a"], produces=["data"],
                  effectiveness_score=1.0)
        vault.save_skill(s)
        crossed = decay_effectiveness(s, status="partial", vault=vault)
        assert s.effectiveness_score == pytest.approx(0.5)
        # crossed=True because we went from >=0.5 to <0.5? actually 0.5 is the
        # threshold itself; the implementation uses strict less-than, so:
        # 1.0 -> 0.5 means new_score (0.5) is NOT < 0.5 → crossed=False.
        assert crossed is False

    def test_threshold_crossing_triggers(self, vault):
        from systemu.core.models import Skill
        from systemu.pipelines.skill_recalibrator import decay_effectiveness

        s = Skill(id="s_decay", name="x", description="y",
                  target_outcomes=["a"], produces=["data"],
                  effectiveness_score=0.6)
        vault.save_skill(s)
        # 0.6 - 0.2 = 0.4 which IS < 0.5 → crossed=True
        crossed = decay_effectiveness(s, status="failure", vault=vault)
        assert s.effectiveness_score == pytest.approx(0.4)
        assert crossed is True

    def test_unknown_status_no_decay(self, vault):
        from systemu.core.models import Skill
        from systemu.pipelines.skill_recalibrator import decay_effectiveness

        s = Skill(id="s_decay", name="x", description="y",
                  target_outcomes=["a"], produces=["data"],
                  effectiveness_score=0.7)
        vault.save_skill(s)
        crossed = decay_effectiveness(s, status="success", vault=vault)
        assert s.effectiveness_score == pytest.approx(0.7)
        assert crossed is False

    def test_floor_at_zero(self, vault):
        from systemu.core.models import Skill
        from systemu.pipelines.skill_recalibrator import decay_effectiveness

        s = Skill(id="s_decay", name="x", description="y",
                  target_outcomes=["a"], produces=["data"],
                  effectiveness_score=0.1)
        vault.save_skill(s)
        decay_effectiveness(s, status="partial", vault=vault)
        assert s.effectiveness_score == 0.0    # clamped, not negative


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor action vocabulary

class TestSupervisorAction:
    def test_recalibrate_skill_in_vocabulary(self):
        from systemu.runtime.execution_mind import ACTION_VOCABULARY, HIGH_IMPACT_ACTIONS
        assert "RECALIBRATE_SKILL" in ACTION_VOCABULARY
        assert "RECALIBRATE_SKILL" in HIGH_IMPACT_ACTIONS


# ─────────────────────────────────────────────────────────────────────────────
# Config knob

class TestConfigKnob:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_AUTO_APPROVE_LOW_RISK_SKILL_RECAL", raising=False)
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.auto_approve_low_risk_skill_recalibrations is False

    def test_env_on(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_AUTO_APPROVE_LOW_RISK_SKILL_RECAL", "true")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.auto_approve_low_risk_skill_recalibrations is True
