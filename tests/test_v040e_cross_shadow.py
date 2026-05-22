"""Tests for v0.4.0-e cross-shadow pattern promotion + refinery dedup.

Covers:
  * detect_and_promote requires ≥ min_shadows distinct signatures
  * within_window filtering
  * idempotency via the promotions ledger
  * global_memory.md gets the promotion appended
  * Refinery dedups against existing signature-bearing entries
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from systemu.pipelines import cross_shadow_patterns as csp
from systemu.vault.vault import Vault


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture
def vault(tmp_path):
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _add_shadow_with_signature(vault, shadow_id: str, signature: str, *,
                                lesson: str = "test lesson", ts_offset_hours: float = 0):
    """Register a shadow + write a buffer entry carrying the signature."""
    # Register in index
    idx = vault.load_index("shadow_army") or []
    idx.append({"id": shadow_id, "name": shadow_id, "status": "awakened"})
    (Path(vault.root) / "shadow_army" / "index.json").write_text(
        json.dumps(idx), encoding="utf-8"
    )
    ts = (datetime.now(tz=timezone.utc) - timedelta(hours=ts_offset_hours)).isoformat(timespec="seconds")
    entry = {
        "category":           "failure_patterns",
        "lesson":             lesson,
        "_pattern_signature": signature,
        "_ts":                ts,
        "evidence_action_blocks": [],
    }
    vault.append_shadow_memory_buffer(shadow_id, entry, source="supervisor_live")


# ─────────────────────────────────────────────────────────────────────────────
# 1) Threshold + window

class TestDetectAndPromote:
    def test_below_threshold_not_promoted(self, vault, tmp_path):
        _add_shadow_with_signature(vault, "sh1", "param_error|tool_x|filename")
        _add_shadow_with_signature(vault, "sh2", "param_error|tool_x|filename")
        # min_shadows=3, only 2 present → no promotion
        result = csp.detect_and_promote(
            vault, min_shadows=3,
            promotions_path=tmp_path / "promotions.json",
        )
        assert result.newly_promoted == []
        assert result.scanned_shadows == 2

    def test_at_threshold_promoted(self, vault, tmp_path):
        sig = "param_error|tool_x|filename"
        for sid in ("sh1", "sh2", "sh3"):
            _add_shadow_with_signature(vault, sid, sig, lesson=f"{sid} lesson")
        result = csp.detect_and_promote(
            vault, min_shadows=3,
            promotions_path=tmp_path / "promotions.json",
        )
        assert len(result.newly_promoted) == 1
        c = result.newly_promoted[0]
        assert c.pattern_signature == sig
        assert sorted(c.shadow_ids) == ["sh1", "sh2", "sh3"]
        # Global memory updated
        global_mem = vault.load_global_memory()
        assert sig in global_mem
        assert "Cross-Shadow Failure Patterns" in global_mem

    def test_outside_window_excluded(self, vault, tmp_path):
        sig = "param_error|tool_x|filename"
        _add_shadow_with_signature(vault, "sh1", sig, ts_offset_hours=24 * 30)  # 30 days ago
        _add_shadow_with_signature(vault, "sh2", sig, ts_offset_hours=24 * 30)
        _add_shadow_with_signature(vault, "sh3", sig, ts_offset_hours=24 * 30)
        result = csp.detect_and_promote(
            vault, min_shadows=3, window_days=7,
            promotions_path=tmp_path / "promotions.json",
        )
        assert result.newly_promoted == []

    def test_idempotent_via_ledger(self, vault, tmp_path):
        sig = "scroll_flaw|unknown|filename"
        ledger = tmp_path / "promotions.json"
        for sid in ("a", "b", "c"):
            _add_shadow_with_signature(vault, sid, sig)
        first = csp.detect_and_promote(vault, min_shadows=3, promotions_path=ledger)
        assert len(first.newly_promoted) == 1

        second = csp.detect_and_promote(vault, min_shadows=3, promotions_path=ledger)
        assert second.newly_promoted == []
        assert sig in second.already_promoted

    def test_dry_run_does_not_mutate(self, vault, tmp_path):
        sig = "param_error|tool_x|kw"
        for sid in ("a", "b", "c"):
            _add_shadow_with_signature(vault, sid, sig)
        ledger = tmp_path / "promotions.json"
        result = csp.detect_and_promote(
            vault, min_shadows=3, promotions_path=ledger, dry_run=True,
        )
        assert len(result.newly_promoted) == 1   # candidate detected
        assert not ledger.exists()                  # ledger NOT written
        assert sig not in vault.load_global_memory()


# ─────────────────────────────────────────────────────────────────────────────
# 2) Refinery signature dedup

class TestRefinerySignatureDedup:
    def test_existing_signature_skipped(self, vault):
        from systemu.pipelines.refinery import _existing_signatures
        _add_shadow_with_signature(vault, "sh1", "param_error|t|kw1", lesson="L1")
        _add_shadow_with_signature(vault, "sh1", "param_error|t|kw2", lesson="L2")
        sigs = _existing_signatures(vault, "sh1")
        assert sigs == {"param_error|t|kw1", "param_error|t|kw2"}

    def test_empty_shadow_returns_empty(self, vault):
        from systemu.pipelines.refinery import _existing_signatures
        # Shadow has no buffer
        assert _existing_signatures(vault, "nonexistent") == set()
