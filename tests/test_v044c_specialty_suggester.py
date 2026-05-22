"""Tests for v0.4.4-c — specialty auto-suggest from consolidated memory.

Covers:
  * Clear winner above threshold → suggested specialty returned
  * Ambiguous matches below threshold → empty suggestion + breakdown
  * No matches at all → empty
  * Word-boundary matching prevents false positives ("doc" inside "docker")
  * Buffer entries' `lesson` text is scanned alongside SHADOW_MEMORY.md
  * Missing shadow / missing memory → graceful empty result
  * Deterministic across runs (no LLM, regex-based)
"""
from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from systemu.runtime.specialty_suggester import (
    SpecialtySuggestion, suggest_specialty,
)


def _vault_with_memory(md_text: str = "", buffer: List[dict] = None):
    v = MagicMock()
    v.load_shadow_memory.return_value = (md_text, buffer or [])
    return v


# ─────────────────────────────────────────────────────────────────────────────

class TestClearWinner:
    def test_browser_specialty_emerges(self):
        md = (
            "Workflow notes: opened browser, navigated to nytimes.com, "
            "took a screenshot of the masthead. Used playwright to click "
            "the menu, then captured DOM nodes for analysis. Browser "
            "automation experience is high."
        )
        v = _vault_with_memory(md)
        result = suggest_specialty("sh-X", vault=v)
        assert result.suggested_specialty == "browser"
        assert result.confidence >= 0.4
        assert result.total_hits >= 5

    def test_data_pipeline_specialty(self):
        md = (
            "Used pandas to load CSV. Transformed dataframe with groupby. "
            "Computed aggregate stats. Exported to parquet. SQL queries "
            "ran against the warehouse. ETL pipeline complete."
        )
        v = _vault_with_memory(md)
        result = suggest_specialty("sh-X", vault=v)
        assert result.suggested_specialty == "data-pipeline"

    def test_buffer_entries_contribute(self):
        # No SHADOW_MEMORY.md but a buffer full of devops lessons
        buffer = [
            {"category": "tool_quirks",
             "lesson": "kubectl rollout failed on stale context; deploy retry needed"},
            {"category": "heuristics",
             "lesson": "Terraform apply on the staging cluster requires fresh credentials"},
            {"category": "failure_patterns",
             "lesson": "Docker build cache stale after Ansible deploy"},
            {"category": "tool_quirks",
             "lesson": "kubectl context switch resets terraform variables"},
            {"category": "heuristics",
             "lesson": "Deploy via github actions; gitlab runner unavailable"},
        ]
        v = _vault_with_memory("", buffer)
        result = suggest_specialty("sh-X", vault=v)
        assert result.suggested_specialty == "devops"


class TestBelowThreshold:
    def test_one_hit_below_threshold(self):
        md = "Used pandas once for an analysis."
        v = _vault_with_memory(md)
        result = suggest_specialty("sh-X", vault=v)
        # 1 hit is below the 5-hit minimum
        assert result.suggested_specialty == ""
        assert result.total_hits == 1

    def test_ambiguous_split_no_winner(self):
        md = (
            # 3 browser hits
            "browser screenshot playwright "
            # 3 devops hits
            "docker kubernetes terraform "
            # 3 data-pipeline hits
            "csv pandas parquet"
        )
        v = _vault_with_memory(md)
        result = suggest_specialty("sh-X", vault=v)
        # 3 hits per specialty, max share is 3/9 ≈ 33% < 40% threshold
        assert result.suggested_specialty == ""
        assert result.total_hits == 9
        # All three specialties present in breakdown
        assert len(result.by_specialty) >= 3


class TestWordBoundary:
    def test_doc_inside_docker_not_counted(self):
        """`docker` should match 'devops' but the substring `doc` (which is
        not even in our keyword list) shouldn't bleed into anything.  This
        guards against a regex written without word boundaries."""
        md = "docker docker docker docker docker docker"
        v = _vault_with_memory(md)
        result = suggest_specialty("sh-X", vault=v)
        assert result.suggested_specialty == "devops"
        # Crucially, document-generation shouldn't appear since "doc" isn't
        # actually one of our keywords (and even if it were, docker should
        # not match it).
        assert "document-generation" not in result.by_specialty or \
               result.by_specialty.get("document-generation", 0) == 0


class TestEmptyInputs:
    def test_no_memory_returns_empty(self):
        v = _vault_with_memory("", [])
        result = suggest_specialty("sh-X", vault=v)
        assert result.suggested_specialty == ""
        assert result.total_hits == 0

    def test_load_failure_returns_empty(self):
        v = MagicMock()
        v.load_shadow_memory.side_effect = RuntimeError("vault broken")
        result = suggest_specialty("sh-X", vault=v)
        assert result.suggested_specialty == ""
        assert result.sources_scanned == 0


class TestDeterminism:
    def test_two_calls_agree(self):
        md = "browser browser browser screenshot dom dom dom dom"
        v = _vault_with_memory(md)
        r1 = suggest_specialty("sh-X", vault=v)
        r2 = suggest_specialty("sh-X", vault=v)
        assert r1.suggested_specialty == r2.suggested_specialty
        assert r1.total_hits == r2.total_hits
        assert r1.by_specialty == r2.by_specialty

    def test_tie_broken_lexically(self):
        """When two specialties tie on count, sort by name ASC."""
        # Build text where 'browser' and 'devops' will tie at 5 each.
        md = (
            "browser screenshot playwright dom click "    # 5 browser
            "docker kubernetes terraform deploy ci "      # 5 devops
        )
        v = _vault_with_memory(md)
        result = suggest_specialty("sh-X", vault=v)
        # browser comes before devops lexically; 5 / 10 = 50% (above 40%
        # threshold, so a suggestion IS returned), and the tie-break goes
        # to "browser".
        assert result.suggested_specialty == "browser"
