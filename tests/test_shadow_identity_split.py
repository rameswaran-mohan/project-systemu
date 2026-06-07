"""Unit tests for the v0.3 Shadow identity split.

Covers:
    * The Pydantic model migrates legacy ``system_prompt`` → ``identity_block``
      transparently on load.
    * ``system_prompt`` is a computed property composed from both fields.
    * Round-tripping a Shadow through JSON preserves both fields.
    * The Workshop-side write path goes to ``identity_block`` only;
      ``accumulated_voice`` stays untouched.
    * The Evolution Engine's shadow upgrade only modifies
      ``identity_block`` (consolidator-owned ``accumulated_voice`` is
      preserved).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.core.models import Shadow, ShadowStatus


# ── Pydantic model behaviour ────────────────────────────────────────────

def test_legacy_system_prompt_migrates_to_identity_block():
    """Pre-v0.3 JSON had only a ``system_prompt`` field.  Loading such a
    Shadow today must transfer that value into ``identity_block`` and
    leave ``accumulated_voice`` empty."""
    s = Shadow.model_validate({
        "id": "sh-1", "name": "Legacy", "description": "test",
        "system_prompt": "You are a helpful tester.",
    })
    assert s.identity_block == "You are a helpful tester."
    assert s.accumulated_voice == ""
    # The computed system_prompt is the same string (no voice to append).
    assert s.system_prompt == "You are a helpful tester."


def test_new_shape_separate_fields_persisted():
    """v0.3+ shape: identity_block and accumulated_voice are independent."""
    s = Shadow.model_validate({
        "id": "sh-2", "name": "Modern", "description": "test",
        "identity_block": "I am a specialist.",
        "accumulated_voice": "I prefer terse confirmations.",
    })
    assert s.identity_block == "I am a specialist."
    assert s.accumulated_voice == "I prefer terse confirmations."


def test_computed_system_prompt_composes_both_fields():
    """system_prompt = identity_block + blank line + accumulated_voice."""
    s = Shadow(
        id="sh-3", name="Composer", description="test",
        identity_block="Identity here.",
        accumulated_voice="Voice patterns here.",
    )
    assert s.system_prompt == "Identity here.\n\nVoice patterns here."


def test_computed_system_prompt_handles_empty_voice():
    s = Shadow(
        id="sh-4", name="Empty voice", description="test",
        identity_block="Only identity.",
    )
    assert s.system_prompt == "Only identity."


def test_computed_system_prompt_handles_empty_identity():
    s = Shadow(
        id="sh-5", name="Voice only", description="test",
        accumulated_voice="Only voice.",
    )
    assert s.system_prompt == "Only voice."


def test_system_prompt_is_read_only():
    """The computed property has no setter — callers must write to
    identity_block (or accumulated_voice via the consolidator)."""
    s = Shadow(id="sh-6", name="x", description="x", identity_block="orig")
    with pytest.raises(AttributeError):
        s.system_prompt = "new value"  # type: ignore[misc]


def test_round_trip_via_model_dump():
    """Serialising and reloading preserves both split fields."""
    original = Shadow(
        id="sh-7", name="Round trip", description="test",
        identity_block="ID block.",
        accumulated_voice="Voice block.",
    )
    data = original.model_dump(mode="json")
    # The computed field appears in dump output for backwards compat —
    # but reload should not double-apply it.
    assert data["identity_block"] == "ID block."
    assert data["accumulated_voice"] == "Voice block."
    assert data["system_prompt"] == "ID block.\n\nVoice block."

    reloaded = Shadow.model_validate(data)
    assert reloaded.identity_block == "ID block."
    assert reloaded.accumulated_voice == "Voice block."


def test_legacy_value_doesnt_override_new_shape():
    """If both legacy and new fields are present, prefer the new shape.
    (Edge case for partial migrations.)"""
    s = Shadow.model_validate({
        "id": "sh-8", "name": "Mixed", "description": "test",
        "system_prompt": "LEGACY VALUE",
        "identity_block": "MODERN VALUE",
        "accumulated_voice": "voice",
    })
    assert s.identity_block == "MODERN VALUE"
    assert s.accumulated_voice == "voice"


# ── File vault round-trip ───────────────────────────────────────────────

def test_file_vault_round_trip(tmp_path: Path):
    """A Shadow saved + reloaded via the file vault preserves the split."""
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    v = Vault(str(tmp_path))

    sh = Shadow(
        id="sh-rt", name="Round-trip", description="x",
        identity_block="The operator wrote this.",
        accumulated_voice="The consolidator learned this.",
    )
    v.save_shadow(sh)
    reloaded = v.get_shadow("sh-rt")
    assert reloaded.identity_block == "The operator wrote this."
    assert reloaded.accumulated_voice == "The consolidator learned this."
    assert reloaded.system_prompt == (
        "The operator wrote this.\n\nThe consolidator learned this."
    )


def test_file_vault_handles_legacy_shadow_json(tmp_path: Path):
    """Pre-v0.3 shadow.json files (only system_prompt) must still load."""
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    # Hand-roll a legacy shadow.json into the directory the vault expects.
    legacy_dir = tmp_path / "shadow_army" / "shadow_sh-legacy"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "shadow.json").write_text(json.dumps({
        "id": "sh-legacy",
        "name": "Pre-v0.3",
        "description": "Imported from a legacy install",
        "system_prompt": "Old-style persona prompt.",
        "assigned_activity_ids": [],
        "available_tool_ids": [],
        "skill_ids": [],
        "status": "dormant",
        "execution_log": [],
        "evolution_history": [],
        "memory_md_path": "",
        "memory_buffer_path": "",
        "created_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-01T00:00:00",
    }))

    v = Vault(str(tmp_path))
    loaded = v.get_shadow("sh-legacy")
    assert loaded.identity_block == "Old-style persona prompt."
    assert loaded.accumulated_voice == ""
    assert loaded.system_prompt == "Old-style persona prompt."


# ── Evolution Engine targets identity_block, not system_prompt ─────────

def test_evolution_upgrade_only_touches_identity_block(monkeypatch, tmp_path: Path):
    """A shadow UPGRADE evolution must write to identity_block, NOT
    overwrite accumulated_voice.  This is the v0.3 ownership contract."""
    from systemu.vault.vault import Vault
    from sharing_on.config import Config
    from systemu.core.models import Evolution, EvolutionStatus, EvolutionType
    from systemu.pipelines.evolution_engine import apply_evolution

    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    v = Vault(str(tmp_path))

    # Pre-existing shadow with both fields populated.
    sh = Shadow(
        id="sh-evo", name="Evolve me",
        description="test",
        identity_block="ORIGINAL IDENTITY",
        accumulated_voice="CONSOLIDATOR-WRITTEN VOICE",
        status=ShadowStatus.AWAKENED,
    )
    v.save_shadow(sh)

    evolution = Evolution(
        id="evo-1",
        evolution_type=EvolutionType.UPGRADE,
        target_entity_type="shadow",
        target_entity_ids=["sh-evo"],
        description="Add the ability to parse Markdown.",
        rationale="The Shadow needs to handle .md inputs.",
        status=EvolutionStatus.APPROVED,
    )
    v.save_evolution(evolution)

    # Mock the LLM call so we don't hit a real network.  The Evolution
    # Engine does a local `from systemu.core.llm_router import llm_call_json
    # as _llm` inside _apply_shadow_upgrade — patch the source module so
    # the local rebinding picks up the fake.
    def _fake_llm(*args, **kwargs):
        return {"updated_system_prompt": "UPDATED IDENTITY with markdown support"}

    monkeypatch.setattr(
        "systemu.core.llm_router.llm_call_json", _fake_llm
    )

    config = Config()
    config.vault_dir = str(tmp_path)
    # apply_evolution takes the evolution ID, not the object.
    assert apply_evolution(evolution.id, config, v) is True

    reloaded = v.get_shadow("sh-evo")
    assert reloaded.identity_block == "UPDATED IDENTITY with markdown support"
    # The critical assertion — accumulated_voice was NOT touched.
    assert reloaded.accumulated_voice == "CONSOLIDATOR-WRITTEN VOICE"
