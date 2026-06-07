"""Unit tests for the memory-tier write contract (v0.2 + v0.2.2).

Covers:
    * The vault gate-keeper helpers (append_shadow_memory_buffer /
      append_elder_buffer) stamp tier provenance.
    * Cross-tier writes are rejected per Rule 1 of docs/memory-model.md.
    * Missing required fields and `type`/`category` conflicts are caught
      at the boundary.
    * Persisted entries carry the canonical `category` field (the legacy
      `type` alias is dropped).
    * Both file Vault and SqliteVault enforce the same contract via the
      shared `augment_buffer_entry` helper in systemu.core.memory_types.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.vault.vault import Vault
from systemu.core.memory_types import (
    SHADOW_CLAIM_TYPES,
    ELDER_RECOMMENDED_TYPES,
    augment_buffer_entry,
)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """Minimal vault layout — just enough for the buffer writes."""
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


# ── Canonical-on-`category` writes ──────────────────────────────────────

def test_append_shadow_memory_buffer_stamps_tier(vault: Vault):
    out = vault.append_shadow_memory_buffer(
        "sh-1",
        {"category": "tool_quirks", "lesson": "X"},
        source="runtime",
    )
    assert out["category"] == "tool_quirks"
    assert "type" not in out                  # legacy alias dropped
    assert out["_tier"] == "shadow"
    assert out["_source"] == "runtime"
    assert "_ts" in out


def test_append_shadow_memory_buffer_persists_to_disk(vault: Vault, tmp_path: Path):
    vault.append_shadow_memory_buffer(
        "sh-1",
        {"category": "tool_quirks", "lesson": "X"},
        source="runtime",
    )
    buf_path = tmp_path / "shadow_army" / "shadow_sh-1" / "memory_buffer.jsonl"
    assert buf_path.exists()
    lines = buf_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["category"] == "tool_quirks"
    assert entry["_tier"] == "shadow"


def test_append_shadow_rejects_elder_category(vault: Vault):
    with pytest.raises(ValueError, match="opposite tier"):
        vault.append_shadow_memory_buffer(
            "sh-1",
            {"category": "user_preference", "lesson": "wrong tier"},
            source="runtime",
        )


def test_append_elder_buffer_stamps_tier(vault: Vault):
    out = vault.append_elder_buffer(
        {"category": "Workflow Patterns", "observation": "Y"},
        source="consolidator",
    )
    assert out["category"] == "Workflow Patterns"
    assert out["_tier"] == "elder"
    assert out["_source"] == "consolidator"


def test_append_elder_buffer_persists_to_disk(vault: Vault, tmp_path: Path):
    vault.append_elder_buffer(
        {"category": "Workflow Patterns", "observation": "Y"},
        source="consolidator",
    )
    buf_path = tmp_path / "elder" / "memory_buffer.jsonl"
    assert buf_path.exists()
    entry = json.loads(buf_path.read_text(encoding="utf-8").strip())
    assert entry["_tier"] == "elder"


def test_append_elder_rejects_shadow_category(vault: Vault):
    with pytest.raises(ValueError, match="opposite tier"):
        vault.append_elder_buffer(
            {"category": "tool_quirks", "observation": "wrong tier"},
            source="consolidator",
        )


# ── Validation: missing/invalid discriminator ────────────────────────────

def test_missing_category_field_is_rejected(vault: Vault):
    with pytest.raises(ValueError, match="missing 'category'"):
        vault.append_shadow_memory_buffer(
            "sh-1", {"lesson": "no category"}, source="runtime",
        )


def test_non_dict_entry_is_rejected(vault: Vault):
    with pytest.raises(ValueError, match="must be a dict"):
        vault.append_shadow_memory_buffer(
            "sh-1", "not a dict", source="runtime",  # type: ignore[arg-type]
        )


def test_conflicting_type_and_category_is_rejected(vault: Vault):
    """Setting both fields to different values is a footgun — reject it."""
    with pytest.raises(ValueError, match="conflicting"):
        vault.append_shadow_memory_buffer(
            "sh-1",
            {"type": "tool_quirks", "category": "heuristics", "lesson": "x"},
            source="runtime",
        )


def test_matching_type_and_category_is_accepted(vault: Vault):
    """If type and category agree, the entry passes (type is dropped)."""
    out = vault.append_shadow_memory_buffer(
        "sh-1",
        {"type": "tool_quirks", "category": "tool_quirks", "lesson": "x"},
        source="runtime",
    )
    assert out["category"] == "tool_quirks"
    assert "type" not in out


def test_legacy_type_alias_accepted(vault: Vault):
    """Pre-canonicalisation callers using `type` instead of `category`
    still work — the value transfers to `category`, the alias is dropped."""
    out = vault.append_shadow_memory_buffer(
        "sh-1",
        {"type": "tool_quirks", "lesson": "x"},
        source="legacy_caller",
    )
    assert out["category"] == "tool_quirks"
    assert "type" not in out


# ── Strict mode (Shadow tier only) ──────────────────────────────────────

def test_unrecognised_shadow_category_rejected_under_strict_mode(vault: Vault):
    """Default behaviour after the v0.2.2 audit: strict mode is on, so a
    novel category that isn't in SHADOW_CLAIM_TYPES is rejected."""
    with pytest.raises(ValueError, match="unrecognised"):
        vault.append_shadow_memory_buffer(
            "sh-1", {"category": "novel_category", "lesson": "X"}, source="runtime",
        )


def test_strict_mode_off_accepts_unknown_shadow_category(tmp_path: Path):
    """Operators replaying pre-audit data can opt out via the constructor."""
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
    permissive_vault = Vault(str(tmp_path), strict_tier_types=False)
    out = permissive_vault.append_shadow_memory_buffer(
        "sh-1", {"category": "ad_hoc_category", "lesson": "X"}, source="runtime",
    )
    assert out["category"] == "ad_hoc_category"


# ── Production entry shapes (refinery + evolution_engine) ──────────────

def test_refinery_production_entry_shape_accepted(vault: Vault):
    """Exact entry shape that pipelines/refinery.py writes after the migration."""
    entry = {
        "created_at":             "2026-05-12T10:00:00",
        "exec_id":                "exec_abc123",
        "category":               "heuristics",
        "lesson":                 "When the user says 'quick summary', limit output to 5 bullets.",
        "evidence_action_blocks": [],
    }
    out = vault.append_shadow_memory_buffer("sh-1", entry, source="refinery")
    assert out["_tier"] == "shadow"
    assert out["_source"] == "refinery"
    assert out["category"] == "heuristics"
    assert out["lesson"].startswith("When the user")


def test_evolution_engine_production_entry_shape_accepted(vault: Vault):
    """Exact entry shape that pipelines/evolution_engine.py writes."""
    entry = {
        "category":    "Workflow Patterns",
        "observation": "User always re-orders files alphabetically after listing.",
        "confidence":  0.8,
        "shadow_id":   "sh-1",
        "exec_id":     "exec_xyz789",
        "timestamp":   "2026-05-12T10:00:00",
    }
    out = vault.append_elder_buffer(entry, source="evolution_engine")
    assert out["_tier"] == "elder"
    assert out["_source"] == "evolution_engine"


@pytest.mark.parametrize("claim_category", sorted(SHADOW_CLAIM_TYPES))
def test_every_canonical_shadow_category_accepted(vault: Vault, claim_category: str):
    """Every value in SHADOW_CLAIM_TYPES must round-trip through the helper."""
    out = vault.append_shadow_memory_buffer(
        "sh-1", {"category": claim_category, "lesson": "x"}, source="refinery",
    )
    assert out["category"] == claim_category


# ── Elder open-endedness — pin the contract ─────────────────────────────

@pytest.mark.parametrize("llm_emitted_category", [
    "Workflow Patterns", "workflow_patterns", "user_preference",
    "Pattern", "Communication Style", "Time Of Day",
    "ad-hoc-llm-string-with-dashes", "ALL_CAPS",
    "spaced  multiple   words", "Émoji 🌙 unicode",
])
def test_elder_accepts_any_non_shadow_category(
    vault: Vault, llm_emitted_category: str,
):
    """Pin the open-endedness contract: anything the LLM emits as an
    Elder category must pass, as long as it doesn't collide with a
    Shadow-tier name."""
    out = vault.append_elder_buffer(
        {"category": llm_emitted_category, "observation": "x"},
        source="evolution_engine",
    )
    assert out["_tier"] == "elder"
    assert out["category"] == llm_emitted_category


@pytest.mark.parametrize("shadow_category", sorted(SHADOW_CLAIM_TYPES))
def test_elder_rejects_any_shadow_category(
    vault: Vault, shadow_category: str,
):
    """Cross-tier wall is symmetric: every Shadow-tier name must be
    rejected from the Elder buffer."""
    with pytest.raises(ValueError, match="opposite tier"):
        vault.append_elder_buffer(
            {"category": shadow_category, "observation": "x"},
            source="evolution_engine",
        )


# ── Caller dict isolation ───────────────────────────────────────────────

def test_caller_dict_not_mutated(vault: Vault):
    """The helper takes a shallow copy — caller's dict stays clean."""
    original = {"category": "tool_quirks", "lesson": "X"}
    vault.append_shadow_memory_buffer("sh-1", original, source="runtime")
    assert original == {"category": "tool_quirks", "lesson": "X"}
    assert "_tier" not in original


# ── Pure-function reachability + symmetry ───────────────────────────────

def test_augment_buffer_entry_is_pure_and_importable():
    """The shared augment_buffer_entry should be reachable as a top-level
    export from systemu.core.memory_types — guarantees the vault and
    SqliteVault can both import it for identical validation."""
    from systemu.core.memory_types import augment_buffer_entry as fn
    out = fn(
        {"category": "tool_quirks", "lesson": "x"},
        tier="shadow",
        source="test",
        allowed=SHADOW_CLAIM_TYPES,
        forbidden=ELDER_RECOMMENDED_TYPES,
        strict=True,
    )
    assert out["category"] == "tool_quirks"
    assert out["_tier"] == "shadow"


def test_sqlite_vault_uses_same_contract(tmp_path: Path):
    """SqliteVault must enforce the same validation rules as the file
    Vault — they both delegate to augment_buffer_entry."""
    try:
        from systemu.storage.sqlite.vault import SqliteVault
    except ImportError:
        pytest.skip("SqliteVault requires sqlalchemy")

    db_path = tmp_path / "test.db"
    sv = SqliteVault(f"sqlite:///{db_path.as_posix()}")
    # Cross-tier rejection should fire identically
    with pytest.raises(ValueError, match="opposite tier"):
        sv.append_shadow_memory_buffer(
            "sh-x", {"category": "user_preference", "lesson": "wrong"},
            source="test",
        )
    # Strict mode rejection should fire identically
    with pytest.raises(ValueError, match="unrecognised"):
        sv.append_shadow_memory_buffer(
            "sh-x", {"category": "made_up", "lesson": "x"},
            source="test",
        )
    # Valid Shadow write should succeed
    out = sv.append_shadow_memory_buffer(
        "sh-x", {"category": "tool_quirks", "lesson": "x"}, source="test",
    )
    assert out["category"] == "tool_quirks"
    assert out["_tier"] == "shadow"
