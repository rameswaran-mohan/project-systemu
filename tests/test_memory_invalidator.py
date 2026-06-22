"""Tests for systemu.runtime.memory_invalidator.

Covers the v0.3.4 hook that writes a contradicting ``failure_patterns``
entry to a Shadow's memory buffer when a previously-dep-failed tool
starts succeeding.

The detector is intentionally conservative: it fires only when the
buffer has an obvious dep-failure lesson for the tool, OR when the
caller signals ``previously_failed=True``.  Both paths are tested,
plus idempotency.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.core.models import Shadow, ShadowStatus
from systemu.runtime import memory_invalidator as mi
from systemu.vault.vault import Vault


@pytest.fixture(autouse=True)
def _reset():
    mi.reset_for_tests()
    yield
    mi.reset_for_tests()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """Minimal on-disk vault layout for buffer writes."""
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _make_shadow(shadow_id="sh-1") -> Shadow:
    return Shadow(
        id=shadow_id,
        name="TestShadow",
        description="for tests",
        system_prompt="-",
        status=ShadowStatus.AWAKENED,
    )


def _read_buffer(vault, shadow_id):
    _md, entries = vault.load_shadow_memory(shadow_id)
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Stale-lesson detection

class TestFiresOnStaleLesson:
    def test_fires_when_buffer_has_dep_failure_lesson(self, vault):
        shadow = _make_shadow()
        vault.append_shadow_memory_buffer(
            shadow.id,
            {
                "category": "failure_patterns",
                "lesson": (
                    "When 'docx' module is missing and cannot be installed, "
                    "consider alternative formats like PDF for tool create_word_doc."
                ),
                "evidence_action_blocks": [],
            },
            source="refinery",
        )
        wrote = mi.maybe_invalidate_dep_lesson(
            vault, shadow, "create_word_doc",
            previously_failed=False,
        )
        assert wrote is True
        entries = _read_buffer(vault, shadow.id)
        assert any(
            e.get("_invalidates") == ["create_word_doc"]
            for e in entries
        )

    def test_no_fire_when_buffer_unrelated(self, vault):
        shadow = _make_shadow()
        vault.append_shadow_memory_buffer(
            shadow.id,
            {
                "category": "heuristics",
                "lesson": "Prefer JSON over YAML for config.",
                "evidence_action_blocks": [],
            },
            source="refinery",
        )
        wrote = mi.maybe_invalidate_dep_lesson(
            vault, shadow, "create_word_doc",
            previously_failed=False,
        )
        assert wrote is False
        # No new entries written.
        entries = _read_buffer(vault, shadow.id)
        assert len(entries) == 1

    def test_fires_when_signal_only_from_run(self, vault):
        """Even with an empty buffer, an in-run signal should fire the
        invalidation (covers the case where the bad lesson hasn't yet
        been refined to disk)."""
        shadow = _make_shadow()
        wrote = mi.maybe_invalidate_dep_lesson(
            vault, shadow, "create_word_doc",
            previously_failed=True,
            execution_id="exec_xyz",
        )
        assert wrote is True
        entries = _read_buffer(vault, shadow.id)
        assert len(entries) == 1
        assert entries[0]["_invalidates"] == ["create_word_doc"]
        assert entries[0]["_execution_id"] == "exec_xyz"


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency

class TestIdempotent:
    def test_second_call_skipped_via_process_cache(self, vault):
        shadow = _make_shadow()
        vault.append_shadow_memory_buffer(
            shadow.id,
            {
                "category": "failure_patterns",
                "lesson": "missing docx module for tool create_word_doc",
                "evidence_action_blocks": [],
            },
            source="refinery",
        )
        assert mi.maybe_invalidate_dep_lesson(
            vault, shadow, "create_word_doc", previously_failed=False,
        )
        # Second call: in-process cache short-circuits, no second write.
        assert not mi.maybe_invalidate_dep_lesson(
            vault, shadow, "create_word_doc", previously_failed=False,
        )
        invalidations = [
            e for e in _read_buffer(vault, shadow.id)
            if "create_word_doc" in (e.get("_invalidates") or [])
        ]
        assert len(invalidations) == 1

    def test_existing_invalidation_in_buffer_blocks_new(self, vault):
        """Pre-existing invalidation entries on disk (from a prior process
        that restarted) should prevent a second one."""
        shadow = _make_shadow()
        # Bad lesson.
        vault.append_shadow_memory_buffer(
            shadow.id,
            {
                "category": "failure_patterns",
                "lesson": "missing docx module for create_word_doc",
                "evidence_action_blocks": [],
            },
            source="refinery",
        )
        # Already-written invalidation (simulating a prior process).
        vault.append_shadow_memory_buffer(
            shadow.id,
            {
                "category": "failure_patterns",
                "lesson": "Tool 'create_word_doc' resolved",
                "evidence_action_blocks": [],
                "_invalidates": ["create_word_doc"],
                "_resolved_via": "dep_installer",
            },
            source="dep_resolved",
        )
        # The de-dup cache is per-process and empty, so we'd write again
        # if the disk check didn't catch it.  It MUST catch it.
        wrote = mi.maybe_invalidate_dep_lesson(
            vault, shadow, "create_word_doc", previously_failed=False,
        )
        assert wrote is False
        invalidations = [
            e for e in _read_buffer(vault, shadow.id)
            if "create_word_doc" in (e.get("_invalidates") or [])
        ]
        assert len(invalidations) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Detection internals

class TestFindStaleDepLessons:
    def test_matches_tool_name_and_keyword(self):
        entries = [
            {"category": "failure_patterns",
             "lesson": "create_word_doc had missing module"},
            {"category": "failure_patterns",
             "lesson": "do not use create_word_doc on Fridays"},   # no dep keyword
            {"category": "heuristics",
             "lesson": "create_word_doc is missing module"},        # wrong category
        ]
        idx = mi._find_stale_dep_lessons(entries, "create_word_doc")
        assert idx == [0]

    def test_skips_existing_invalidations(self):
        entries = [
            {"category": "failure_patterns",
             "lesson": "create_word_doc had missing module"},
            {"category": "failure_patterns",
             "lesson": "create_word_doc resolved",
             "_invalidates": ["create_word_doc"]},
        ]
        idx = mi._find_stale_dep_lessons(entries, "create_word_doc")
        assert idx == [0]   # invalidation at index 1 not flagged as stale
