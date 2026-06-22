"""Tests for v0.4.2-a — affinity-aware routing in Supervisor.submit().

Covers ``_resolve_shadow_with_affinity`` directly (no need to spin up
the full Supervisor loop for routing logic):

* No affinity hit + no exclusion → returns original shadow
* Affinity-log TERMINATE → swaps to an alternative
* Caller exclusion → swaps to an alternative
* No alternative available → falls back to original (warn, don't crash)
* Skill-overlap scoring picks the best match
* Excluded alternatives are skipped
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.runtime import affinity_log as al
from systemu.runtime.affinity_log import AffinityLog
from systemu.runtime.supervisor import Supervisor


@pytest.fixture(autouse=True)
def _reset_affinity():
    al.reset_singleton_for_tests()
    yield
    al.reset_singleton_for_tests()


def _make_supervisor_stub(*, vault):
    """Build a minimal stub satisfying _resolve_shadow_with_affinity."""
    sup = Supervisor.__new__(Supervisor)
    sup.vault = vault
    return sup


def _activity(id_="act-1", scroll_id="scroll-1", required_skills=None):
    return SimpleNamespace(
        id=id_,
        scroll_id=scroll_id,
        required_skill_ids=list(required_skills or []),
    )


def _scroll(id_="scroll-1", intent="Generate report", objectives=None):
    return SimpleNamespace(
        id=id_, intent=intent,
        objectives=objectives or [SimpleNamespace(goal="fetch"), SimpleNamespace(goal="format")],
    )


def _shadow(id_, skills=None):
    return {"id": id_, "skill_ids": list(skills or [])}


# ─────────────────────────────────────────────────────────────────────────────

class TestNoSwapNeeded:
    def test_no_affinity_hit_no_exclusion_returns_original(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        vault = MagicMock()
        vault.get_activity.return_value = _activity()
        vault.get_scroll.return_value = _scroll()
        vault.list_shadows.return_value = [_shadow("sh-A"), _shadow("sh-B")]
        sup = _make_supervisor_stub(vault=vault)

        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id=None, scroll_id="scroll-1",
        )
        assert result == "sh-A"


class TestAffinitySwap:
    def test_terminated_shadow_swapped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        # Record TERMINATE for (intent_hash, sh-A)
        scroll = _scroll()
        from systemu.runtime.affinity_log import compute_intent_hash, get_affinity_log
        ih = compute_intent_hash(intent=scroll.intent, objectives=scroll.objectives)
        get_affinity_log().record_termination(intent_hash=ih, shadow_id="sh-A")

        vault = MagicMock()
        vault.get_activity.return_value = _activity(required_skills=["skill_x"])
        vault.get_scroll.return_value = scroll
        vault.list_shadows.return_value = [
            _shadow("sh-A", skills=["skill_x"]),
            _shadow("sh-B", skills=["skill_x"]),
        ]
        sup = _make_supervisor_stub(vault=vault)

        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id=None, scroll_id="scroll-1",
        )
        assert result == "sh-B"

    def test_caller_exclusion_swapped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        vault = MagicMock()
        vault.get_activity.return_value = _activity(required_skills=["skill_x"])
        vault.get_scroll.return_value = _scroll()
        vault.list_shadows.return_value = [
            _shadow("sh-A", skills=["skill_x"]),
            _shadow("sh-B", skills=["skill_x"]),
        ]
        sup = _make_supervisor_stub(vault=vault)

        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scroll-1",
        )
        assert result == "sh-B"


class TestNoAlternative:
    def test_no_match_falls_back_to_original(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        vault = MagicMock()
        vault.get_activity.return_value = _activity(required_skills=["skill_x"])
        vault.get_scroll.return_value = _scroll()
        # Only sh-A is in the army; if it's excluded, no alternatives exist.
        vault.list_shadows.return_value = [_shadow("sh-A", skills=["skill_x"])]
        sup = _make_supervisor_stub(vault=vault)

        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scroll-1",
        )
        # Fall-back: keep the original assignment rather than dropping.
        assert result == "sh-A"

    def test_alternatives_without_skill_overlap_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        vault = MagicMock()
        vault.get_activity.return_value = _activity(required_skills=["skill_x"])
        vault.get_scroll.return_value = _scroll()
        vault.list_shadows.return_value = [
            _shadow("sh-A", skills=["skill_x"]),
            _shadow("sh-B", skills=["skill_y"]),    # mismatch
        ]
        sup = _make_supervisor_stub(vault=vault)

        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scroll-1",
        )
        # No skill-overlap alternative → fall back to original.
        assert result == "sh-A"


class TestSkillScoring:
    def test_higher_overlap_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        vault = MagicMock()
        vault.get_activity.return_value = _activity(
            required_skills=["skill_x", "skill_y", "skill_z"],
        )
        vault.get_scroll.return_value = _scroll()
        vault.list_shadows.return_value = [
            _shadow("sh-A", skills=["skill_x"]),
            _shadow("sh-B", skills=["skill_x", "skill_y"]),         # better
            _shadow("sh-C", skills=["skill_x", "skill_y", "skill_z"]),  # best
        ]
        sup = _make_supervisor_stub(vault=vault)

        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scroll-1",
        )
        assert result == "sh-C"

    def test_alternative_also_excluded_by_affinity_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        scroll = _scroll()
        from systemu.runtime.affinity_log import compute_intent_hash, get_affinity_log
        ih = compute_intent_hash(intent=scroll.intent, objectives=scroll.objectives)
        # Both sh-A and sh-B have TERMINATEd recently — sh-C is the only viable choice.
        get_affinity_log().record_termination(intent_hash=ih, shadow_id="sh-A")
        get_affinity_log().record_termination(intent_hash=ih, shadow_id="sh-B")
        vault = MagicMock()
        vault.get_activity.return_value = _activity(required_skills=["skill_x"])
        vault.get_scroll.return_value = scroll
        vault.list_shadows.return_value = [
            _shadow("sh-A", skills=["skill_x"]),
            _shadow("sh-B", skills=["skill_x"]),
            _shadow("sh-C", skills=["skill_x"]),
        ]
        sup = _make_supervisor_stub(vault=vault)
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id=None, scroll_id="scroll-1",
        )
        assert result == "sh-C"


class TestNoActivityScrollOrFailures:
    def test_missing_activity_returns_original(self):
        vault = MagicMock()
        vault.get_activity.side_effect = KeyError("nope")
        sup = _make_supervisor_stub(vault=vault)
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id=None, scroll_id="scroll-1",
        )
        assert result == "sh-A"

    def test_no_required_skills_scores_all_alternatives(self, tmp_path, monkeypatch):
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        vault = MagicMock()
        vault.get_activity.return_value = _activity(required_skills=[])
        vault.get_scroll.return_value = _scroll()
        vault.list_shadows.return_value = [
            _shadow("sh-A", skills=["s1"]),
            _shadow("sh-B", skills=[]),
            _shadow("sh-C", skills=[]),
        ]
        sup = _make_supervisor_stub(vault=vault)
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scroll-1",
        )
        # No required skills → all alternatives equally scored; sorted by id → "sh-B".
        assert result == "sh-B"
