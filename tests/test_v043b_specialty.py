"""Tests for v0.4.3-b — Shadow.specialty + routing preference.

Covers:
  * Shadow model gains specialty (defaults to "")
  * Vault round-trip preserves specialty
  * Pre-v0.4.3 shadow JSON without the field defaults to ""
  * Affinity router prefers candidates with matching specialty as a
    third-tier ranking (after skill_overlap, after specialty match,
    success_rate becomes the next tiebreaker)
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.core.models import Shadow, ShadowStatus
from systemu.runtime import affinity_log as al
from systemu.runtime import shadow_metrics as sm


@pytest.fixture(autouse=True)
def _reset():
    al.reset_singleton_for_tests()
    sm.reset_singleton_for_tests()
    yield
    al.reset_singleton_for_tests()
    sm.reset_singleton_for_tests()


# ─────────────────────────────────────────────────────────────────────────────
# Model + vault round-trip

class TestModel:
    def test_default_empty_string(self):
        sh = Shadow(id="sh-1", name="X", description="t")
        assert sh.specialty == ""

    def test_explicit_value(self):
        sh = Shadow(id="sh-1", name="X", description="t", specialty="browser")
        assert sh.specialty == "browser"

    def test_legacy_data_defaults_to_empty(self):
        legacy = {"id": "sh-x", "name": "n", "description": "d"}
        sh = Shadow.model_validate(legacy)
        assert sh.specialty == ""


class TestVaultRoundTrip:
    def test_round_trip(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                    "evolutions"):
            (tmp_path / sub).mkdir()
            (tmp_path / sub / "index.json").write_text("[]")
        v = Vault(str(tmp_path))
        sh = Shadow(
            id="sh-1", name="X", description="t",
            specialty="data-pipeline",
            status=ShadowStatus.AWAKENED,
        )
        v.save_shadow(sh)
        loaded = v.get_shadow("sh-1")
        assert loaded.specialty == "data-pipeline"


# ─────────────────────────────────────────────────────────────────────────────
# Router preference

class TestSpecialtyRoutingPreference:
    def test_matching_specialty_wins_tie(self, tmp_path, monkeypatch):
        """Two candidates with equal skill overlap and no metric history —
        the one whose specialty matches the originating shadow wins."""
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        monkeypatch.setattr(sm, "_DEFAULT_PATH", tmp_path / "metrics.json")
        vault = MagicMock()
        vault.get_activity.return_value = SimpleNamespace(
            id="act-1", scroll_id="scr-1",
            required_skill_ids=["skill_x"],
        )
        vault.get_scroll.return_value = SimpleNamespace(
            id="scr-1", intent="Generate", objectives=[SimpleNamespace(goal="g")],
        )
        vault.list_shadows.return_value = [
            {"id": "sh-A", "skill_ids": ["skill_x"], "specialty": "data-pipeline"},
            {"id": "sh-B", "skill_ids": ["skill_x"], "specialty": "browser"},
            {"id": "sh-C", "skill_ids": ["skill_x"], "specialty": "data-pipeline"},
        ]
        # Originating shadow has specialty=data-pipeline
        origin = SimpleNamespace(id="sh-A", specialty="data-pipeline")
        vault.get_shadow.return_value = origin

        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.vault = vault
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scr-1",
        )
        # sh-C has matching specialty (data-pipeline); sh-B doesn't.
        assert result == "sh-C"

    def test_no_match_falls_back_to_metrics(self, tmp_path, monkeypatch):
        """When no candidate matches the originating specialty, the
        ranking falls through to success_rate as before."""
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        monkeypatch.setattr(sm, "_DEFAULT_PATH", tmp_path / "metrics.json")
        # Seed metrics: sh-C has perfect history on this intent_hash
        from systemu.runtime.affinity_log import compute_intent_hash
        from systemu.runtime.shadow_metrics import get_shadow_metrics
        ih = compute_intent_hash(
            intent="Generate",
            objectives=[SimpleNamespace(goal="g")],
        )
        ms = get_shadow_metrics()
        for _ in range(5):
            ms.record(shadow_id="sh-C", intent_hash=ih, status="success")

        vault = MagicMock()
        vault.get_activity.return_value = SimpleNamespace(
            id="act-1", scroll_id="scr-1",
            required_skill_ids=["skill_x"],
        )
        vault.get_scroll.return_value = SimpleNamespace(
            id="scr-1", intent="Generate", objectives=[SimpleNamespace(goal="g")],
        )
        vault.list_shadows.return_value = [
            {"id": "sh-A", "skill_ids": ["skill_x"], "specialty": "data-pipeline"},
            {"id": "sh-B", "skill_ids": ["skill_x"], "specialty": "browser"},
            {"id": "sh-C", "skill_ids": ["skill_x"], "specialty": "devops"},
        ]
        origin = SimpleNamespace(id="sh-A", specialty="data-pipeline")
        vault.get_shadow.return_value = origin

        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.vault = vault
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scr-1",
        )
        # No specialty match among sh-B/sh-C → fall through to metrics → sh-C wins
        assert result == "sh-C"

    def test_empty_origin_specialty_no_preference(self, tmp_path, monkeypatch):
        """When the originating shadow has no specialty, the specialty
        signal is neutral — ranking falls back to metrics."""
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        monkeypatch.setattr(sm, "_DEFAULT_PATH", tmp_path / "metrics.json")
        vault = MagicMock()
        vault.get_activity.return_value = SimpleNamespace(
            id="act-1", scroll_id="scr-1",
            required_skill_ids=["skill_x"],
        )
        vault.get_scroll.return_value = SimpleNamespace(
            id="scr-1", intent="X", objectives=[SimpleNamespace(goal="g")],
        )
        vault.list_shadows.return_value = [
            {"id": "sh-A", "skill_ids": ["skill_x"], "specialty": ""},
            {"id": "sh-B", "skill_ids": ["skill_x"], "specialty": "browser"},
            {"id": "sh-C", "skill_ids": ["skill_x"], "specialty": "devops"},
        ]
        origin = SimpleNamespace(id="sh-A", specialty="")
        vault.get_shadow.return_value = origin

        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.vault = vault
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scr-1",
        )
        # Both candidates have neutral metrics → ranking falls to id sort → sh-B.
        assert result == "sh-B"

    def test_skill_overlap_still_dominates_specialty(self, tmp_path, monkeypatch):
        """A 2-skill candidate with wrong specialty beats a 1-skill
        candidate with matching specialty.  Capability comes first."""
        monkeypatch.setattr(al, "_DEFAULT_PATH", tmp_path / "affinity.json")
        monkeypatch.setattr(sm, "_DEFAULT_PATH", tmp_path / "metrics.json")
        vault = MagicMock()
        vault.get_activity.return_value = SimpleNamespace(
            id="act-1", scroll_id="scr-1",
            required_skill_ids=["skill_x", "skill_y"],
        )
        vault.get_scroll.return_value = SimpleNamespace(
            id="scr-1", intent="X", objectives=[SimpleNamespace(goal="g")],
        )
        vault.list_shadows.return_value = [
            {"id": "sh-A", "skill_ids": ["skill_x", "skill_y"], "specialty": "data-pipeline"},
            {"id": "sh-B", "skill_ids": ["skill_x"],            "specialty": "data-pipeline"},
            {"id": "sh-C", "skill_ids": ["skill_x", "skill_y"], "specialty": "browser"},
        ]
        origin = SimpleNamespace(id="sh-A", specialty="data-pipeline")
        vault.get_shadow.return_value = origin

        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.vault = vault
        result = sup._resolve_shadow_with_affinity(
            activity_id="act-1", shadow_id="sh-A",
            exclude_shadow_id="sh-A", scroll_id="scr-1",
        )
        # sh-B has matching specialty but only 1-skill overlap;
        # sh-C has 2-skill overlap but wrong specialty.
        # Skill overlap dominates → sh-C wins.
        assert result == "sh-C"
