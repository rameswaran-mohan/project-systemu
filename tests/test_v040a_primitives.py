"""Tests for v0.4.0-a plumbing.

Covers three independently-shippable primitives that v0.4.0-d will depend on:

1. ``pattern_signature()`` — deterministic, lowercase, pipe-separated;
   used to detect cross-shadow failure recurrence.
2. ``Vault.expunge_memory_entry(predicate)`` — operator-driven removal
   of bad lessons with audit trail.
3. Config knobs read correctly from env vars with sensible defaults.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sharing_on.config import Config
from systemu.core.memory_types import pattern_signature
from systemu.vault.vault import Vault


# ─────────────────────────────────────────────────────────────────────────────
# 1) pattern_signature determinism

class TestPatternSignature:
    def test_basic_three_part(self):
        sig = pattern_signature(
            error_type="missing_dependency",
            tool_name="create_word_doc",
            error_message="No module named 'docx'",
        )
        assert sig == "missing_dependency|create_word_doc|module"

    def test_missing_parts_default_unknown(self):
        sig = pattern_signature(error_type=None, tool_name=None)
        assert sig == "unknown|unknown|unknown"

    def test_explicit_keyword_wins_over_message(self):
        sig = pattern_signature(
            error_type="x", tool_name="y",
            error_message="No module named 'docx'",
            top_keyword="manual_key",
        )
        assert sig.endswith("|manual_key")

    def test_stopwords_skipped(self):
        sig = pattern_signature(
            error_type="e", tool_name="t",
            error_message="The actual problem is foo",
        )
        # "The" / "is" are stopwords → first non-stopword is "actual"
        assert sig.endswith("|actual")

    def test_short_tokens_skipped(self):
        sig = pattern_signature(
            error_type="e", tool_name="t",
            error_message="a b cd efg hij",
        )
        # Regex requires ≥3 chars; first qualifying = "efg"
        assert sig.endswith("|efg")

    def test_signature_capped_at_200(self):
        long_tool = "x" * 500
        sig = pattern_signature(error_type="e", tool_name=long_tool)
        assert len(sig) <= 200

    def test_two_callers_agree(self):
        """Determinism check — different processes computing the signature
        for the same failure must produce identical strings."""
        a = pattern_signature(
            error_type="param_error", tool_name="api_call",
            error_message="Field 'url' missing",
        )
        b = pattern_signature(
            error_type="PARAM_ERROR", tool_name="api_call",
            error_message="Field 'url' missing",
        )
        assert a == b


# ─────────────────────────────────────────────────────────────────────────────
# 2) Vault.expunge_memory_entry

@pytest.fixture
def vault(tmp_path):
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


class TestExpungeMemoryEntry:
    def test_removes_matching_entries(self, vault, tmp_path):
        vault.append_shadow_memory_buffer(
            "sh-1",
            {"category": "failure_patterns", "lesson": "bad lesson A",
             "_source": "supervisor_live"},
            source="supervisor_live",
        )
        vault.append_shadow_memory_buffer(
            "sh-1",
            {"category": "heuristics", "lesson": "good lesson B"},
            source="refinery",
        )
        removed = vault.expunge_memory_entry(
            "sh-1",
            predicate=lambda e: e.get("_source") == "supervisor_live",
            audit_path=tmp_path / "audit.jsonl",
        )
        assert removed == 1
        _md, entries = vault.load_shadow_memory("sh-1")
        assert len(entries) == 1
        assert entries[0]["lesson"] == "good lesson B"

    def test_no_match_returns_zero_and_no_op(self, vault, tmp_path):
        vault.append_shadow_memory_buffer(
            "sh-1",
            {"category": "heuristics", "lesson": "L"},
            source="refinery",
        )
        before = vault.load_shadow_memory("sh-1")[1]
        removed = vault.expunge_memory_entry(
            "sh-1",
            predicate=lambda e: e.get("_source") == "nope",
            audit_path=tmp_path / "audit.jsonl",
        )
        assert removed == 0
        after = vault.load_shadow_memory("sh-1")[1]
        assert before == after

    def test_writes_audit_log(self, vault, tmp_path):
        vault.append_shadow_memory_buffer(
            "sh-1",
            {"category": "failure_patterns", "lesson": "X"},
            source="supervisor_live",
        )
        audit = tmp_path / "audit.jsonl"
        vault.expunge_memory_entry(
            "sh-1",
            predicate=lambda e: e.get("category") == "failure_patterns",
            audit_path=audit,
            reason="test_removal",
        )
        assert audit.exists()
        rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 1
        assert rows[0]["shadow_id"] == "sh-1"
        assert rows[0]["reason"] == "test_removal"
        assert rows[0]["entry"]["category"] == "failure_patterns"

    def test_predicate_exception_keeps_entry(self, vault, tmp_path):
        """Predicate that raises must NOT delete the entry — fail-closed."""
        vault.append_shadow_memory_buffer(
            "sh-1",
            {"category": "failure_patterns", "lesson": "X"},
            source="supervisor_live",
        )
        def bad_pred(e):
            raise RuntimeError("boom")
        removed = vault.expunge_memory_entry(
            "sh-1", predicate=bad_pred,
            audit_path=tmp_path / "audit.jsonl",
        )
        assert removed == 0
        assert len(vault.load_shadow_memory("sh-1")[1]) == 1

    def test_missing_shadow_returns_zero(self, vault, tmp_path):
        # never created shadow
        removed = vault.expunge_memory_entry(
            "nonexistent",
            predicate=lambda e: True,
            audit_path=tmp_path / "audit.jsonl",
        )
        assert removed == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3) Config knobs

class TestSupervisorConfigKnobs:
    def test_defaults(self, monkeypatch):
        # Strip overrides for a clean defaults check.
        for k in (
            "SYSTEMU_MAX_CONSECUTIVE_THINK",
            "SYSTEMU_INTELLIGENT_SUPERVISOR",
            "SYSTEMU_SUPERVISOR_CADENCE",
            "SYSTEMU_SUPERVISOR_BUDGET_RUN",
            "SYSTEMU_SUPERVISOR_TIER_ROUTINE",
            "SYSTEMU_SUPERVISOR_TIER_INTERVENTION",
            "SYSTEMU_SUPERVISOR_TIMEOUT_S",
            "SYSTEMU_SUPERVISOR_BUDGET_HOUR_USD",
            "SYSTEMU_SUPERVISOR_BUDGET_DAY_USD",
        ):
            monkeypatch.delenv(k, raising=False)
        c = Config.from_env()
        assert c.max_consecutive_think == 5
        assert c.intelligent_supervisor_enabled is False
        assert c.supervisor_evaluation_cadence == "auto"
        assert c.supervisor_llm_budget_per_run == 10
        assert c.supervisor_tier_routine == "tier_3"
        assert c.supervisor_tier_intervention == "tier_1"
        assert c.supervisor_directive_timeout_s == 5.0
        assert c.supervisor_llm_budget_per_hour_usd == 5.0
        assert c.supervisor_llm_budget_per_day_usd == 50.0

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_MAX_CONSECUTIVE_THINK", "9")
        monkeypatch.setenv("SYSTEMU_INTELLIGENT_SUPERVISOR", "true")
        monkeypatch.setenv("SYSTEMU_SUPERVISOR_CADENCE", "every_failure")
        monkeypatch.setenv("SYSTEMU_SUPERVISOR_BUDGET_RUN", "25")
        monkeypatch.setenv("SYSTEMU_SUPERVISOR_TIMEOUT_S", "2.5")
        c = Config.from_env()
        assert c.max_consecutive_think == 9
        assert c.intelligent_supervisor_enabled is True
        assert c.supervisor_evaluation_cadence == "every_failure"
        assert c.supervisor_llm_budget_per_run == 25
        assert c.supervisor_directive_timeout_s == 2.5
