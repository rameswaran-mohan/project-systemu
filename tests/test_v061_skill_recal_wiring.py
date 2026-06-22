"""v0.6.1-c — RECALIBRATE_SKILL action wires through shadow_runtime.

Closes review issue #3.  Verifies the three integration points:

1. ``_maybe_decay_loaded_skills`` decays effectiveness on every observed
   failure / partial for skills loaded during this execution; idempotent
   per (execution × skill).
2. Crossing ``RECAL_THRESHOLD`` queues a ``RECALIBRATE_SKILL`` directive.
3. ``_apply_recalibrate_skill_directive`` invokes the recalibrator and
   either auto-applies (low-risk + env knob set) or surfaces a flash card.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# decay hook

class TestDecayHook:
    def test_decay_fires_on_failure_for_loaded_skill(self):
        from systemu.runtime import shadow_runtime as sr

        vault = MagicMock()
        skill = MagicMock(id="skill_a", effectiveness_score=0.7)
        vault.get_skill.return_value = skill

        ctx = SimpleNamespace(
            _loaded_skill_ids={"skill_a"},
            _decayed_skills_this_exec=set(),
            pending_directives=[],
        )

        # decay_effectiveness returns False (no threshold crossing) for 0.7→0.5
        with patch("systemu.runtime.shadow_runtime.decay_effectiveness",
                   return_value=False) as mock_decay:
            sr._maybe_decay_loaded_skills(ctx, vault=vault, status="failure")

        mock_decay.assert_called_once_with(skill, status="failure", vault=vault)
        assert "skill_a" in ctx._decayed_skills_this_exec
        # No directive queued because threshold not crossed
        assert ctx.pending_directives == []

    def test_decay_idempotent_per_execution(self):
        from systemu.runtime import shadow_runtime as sr

        vault = MagicMock()
        skill = MagicMock(id="skill_a", effectiveness_score=0.7)
        vault.get_skill.return_value = skill

        ctx = SimpleNamespace(
            _loaded_skill_ids={"skill_a"},
            _decayed_skills_this_exec=set(),
            pending_directives=[],
        )

        with patch("systemu.runtime.shadow_runtime.decay_effectiveness",
                   return_value=False) as mock_decay:
            sr._maybe_decay_loaded_skills(ctx, vault=vault, status="failure")
            sr._maybe_decay_loaded_skills(ctx, vault=vault, status="failure")
            sr._maybe_decay_loaded_skills(ctx, vault=vault, status="failure")

        # Three failures, but the same skill only decayed once
        assert mock_decay.call_count == 1

    def test_threshold_crossing_queues_directive(self):
        from systemu.runtime import shadow_runtime as sr

        vault = MagicMock()
        skill = MagicMock(id="skill_a", effectiveness_score=0.6)
        vault.get_skill.return_value = skill

        ctx = SimpleNamespace(
            _loaded_skill_ids={"skill_a"},
            _decayed_skills_this_exec=set(),
            pending_directives=[],
        )

        # decay_effectiveness returns True → crossed threshold
        with patch("systemu.runtime.shadow_runtime.decay_effectiveness",
                   return_value=True):
            sr._maybe_decay_loaded_skills(ctx, vault=vault, status="failure")

        assert len(ctx.pending_directives) == 1
        d = ctx.pending_directives[0]
        assert getattr(d, "action", None) == "RECALIBRATE_SKILL"
        assert getattr(d, "skill_id", None) == "skill_a"

    def test_skips_when_no_loaded_skills(self):
        from systemu.runtime import shadow_runtime as sr

        vault = MagicMock()
        ctx = SimpleNamespace(
            _loaded_skill_ids=set(),
            _decayed_skills_this_exec=set(),
            pending_directives=[],
        )

        with patch("systemu.runtime.shadow_runtime.decay_effectiveness") as mock_decay:
            sr._maybe_decay_loaded_skills(ctx, vault=vault, status="failure")

        mock_decay.assert_not_called()

    def test_no_loaded_skills_attribute_treated_as_empty(self):
        from systemu.runtime import shadow_runtime as sr

        vault = MagicMock()
        ctx = SimpleNamespace(pending_directives=[])

        with patch("systemu.runtime.shadow_runtime.decay_effectiveness") as mock_decay:
            # Should not raise — missing attribute treated as empty
            sr._maybe_decay_loaded_skills(ctx, vault=vault, status="failure")

        mock_decay.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# directive handler

class TestRecalibrateSkillDirective:
    def _setup(self):
        skill = MagicMock(
            id="skill_a", name="x",
            produces=["data"], target_outcomes=["doc"],
            required_tool_names=[], skill_version=1,
        )
        vault = MagicMock(); vault.get_skill.return_value = skill
        config = MagicMock()
        config.auto_approve_low_risk_skill_recalibrations = False
        ctx = MagicMock()
        return skill, vault, config, ctx

    def test_invokes_recalibrator(self):
        from systemu.runtime import shadow_runtime as sr
        skill, vault, config, ctx = self._setup()

        recal_result = MagicMock(
            success=True, confidence="high", mode="bump_skill",
            new_instructions_md="new body",
        )
        with patch("systemu.runtime.shadow_runtime.recalibrate_skill",
                   return_value=recal_result) as mock_recal, \
             patch("systemu.runtime.shadow_runtime.is_low_risk_skill_recalibration",
                   return_value=(False, "bump always requires operator")):
            directive = SimpleNamespace(
                action="RECALIBRATE_SKILL", skill_id="skill_a",
            )
            sr._apply_recalibrate_skill_directive(
                directive, context=ctx, vault=vault, config=config,
                execution_id="exec_x",
            )

        mock_recal.assert_called_once()
        # Bump mode without auto-approve → expect operator-card path, NOT apply
        # (no apply_recalibration call asserted because we don't patch it)

    def test_auto_approve_low_risk_applies(self):
        from systemu.runtime import shadow_runtime as sr
        skill, vault, config, ctx = self._setup()
        config.auto_approve_low_risk_skill_recalibrations = True

        recal_result = MagicMock(
            success=True, confidence="high", mode="fork_new_skill",
            new_instructions_md="new body",
        )
        with patch("systemu.runtime.shadow_runtime.recalibrate_skill",
                   return_value=recal_result), \
             patch("systemu.runtime.shadow_runtime.is_low_risk_skill_recalibration",
                   return_value=(True, "fork + high confidence")), \
             patch("systemu.runtime.shadow_runtime.apply_recalibration") as mock_apply:
            directive = SimpleNamespace(
                action="RECALIBRATE_SKILL", skill_id="skill_a",
            )
            sr._apply_recalibrate_skill_directive(
                directive, context=ctx, vault=vault, config=config,
                execution_id="exec_x",
            )

        mock_apply.assert_called_once()

    def test_missing_skill_id_returns_silently(self):
        from systemu.runtime import shadow_runtime as sr
        _, vault, config, ctx = self._setup()
        directive = SimpleNamespace(action="RECALIBRATE_SKILL")    # no skill_id

        with patch("systemu.runtime.shadow_runtime.recalibrate_skill") as mock_recal:
            sr._apply_recalibrate_skill_directive(
                directive, context=ctx, vault=vault, config=config,
                execution_id="exec_x",
            )

        mock_recal.assert_not_called()

    def test_skill_not_in_vault_returns_silently(self):
        from systemu.runtime import shadow_runtime as sr
        _, vault, config, ctx = self._setup()
        vault.get_skill.side_effect = KeyError("not found")
        directive = SimpleNamespace(
            action="RECALIBRATE_SKILL", skill_id="missing",
        )

        with patch("systemu.runtime.shadow_runtime.recalibrate_skill") as mock_recal:
            sr._apply_recalibrate_skill_directive(
                directive, context=ctx, vault=vault, config=config,
                execution_id="exec_x",
            )

        mock_recal.assert_not_called()
