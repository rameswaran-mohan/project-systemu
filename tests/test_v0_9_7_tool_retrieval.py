"""v0.9.7 Phase 0b Task B2 — decision-time tool retrieval.

Tests for systemu.runtime.tool_retrieval.

Key regression check: for location/IP-geolocation queries,
``fetch_json`` must rank ABOVE ``web_search`` because the disambiguated
description in the bundled catalog explicitly mentions IP/geolocation/ip-api.
Conversely, for web-search queries, ``web_search`` must rank above
``fetch_json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.runtime.tool_retrieval import (
    ALWAYS_AVAILABLE,
    ensure_core,
    rank_tools,
)

# ---------------------------------------------------------------------------
# Fixture — load the real catalog
# ---------------------------------------------------------------------------

CATALOG_PATH = Path("systemu/vault/tools/index.json")


@pytest.fixture(scope="module")
def catalog() -> list[dict]:
    """Load the full bundled tool catalog from index.json."""
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _name(tool: dict) -> str:
    return tool.get("name", "")


def _names(tools: list[dict]) -> list[str]:
    return [_name(t) for t in tools]


def _rank_pos(ranked: list[dict], name: str) -> int:
    """Return 0-based position of *name* in *ranked*, or len(ranked) if absent."""
    names = _names(ranked)
    return names.index(name) if name in names else len(ranked)


# ---------------------------------------------------------------------------
# Core regression: IP/location queries → fetch_json wins
# ---------------------------------------------------------------------------

class TestLocationQueryOrdering:
    """fetch_json must beat web_search for IP/location queries."""

    def test_find_my_location(self, catalog):
        ranked = rank_tools("find my location", catalog, k=10)
        fj = _rank_pos(ranked, "fetch_json")
        ws = _rank_pos(ranked, "web_search")
        assert fj < ws, (
            f"fetch_json (pos {fj}) should rank above web_search (pos {ws}) "
            f"for 'find my location'; got: {_names(ranked)}"
        )

    def test_what_city_from_ip(self, catalog):
        ranked = rank_tools("what city am I in from my IP", catalog, k=10)
        fj = _rank_pos(ranked, "fetch_json")
        ws = _rank_pos(ranked, "web_search")
        assert fj < ws, (
            f"fetch_json (pos {fj}) should rank above web_search (pos {ws}) "
            f"for 'what city am I in from my IP'; got: {_names(ranked)}"
        )

    def test_ip_geolocation_query(self, catalog):
        ranked = rank_tools("ip geolocation lookup", catalog, k=10)
        fj = _rank_pos(ranked, "fetch_json")
        ws = _rank_pos(ranked, "web_search")
        assert fj < ws, (
            f"fetch_json (pos {fj}) should rank above web_search (pos {ws}) "
            f"for 'ip geolocation lookup'; got: {_names(ranked)}"
        )


# ---------------------------------------------------------------------------
# Web-search queries → web_search wins
# ---------------------------------------------------------------------------

class TestWebSearchQueryOrdering:
    """web_search must beat fetch_json for web-search queries."""

    def test_search_web_for_news(self, catalog):
        ranked = rank_tools("search the web for news", catalog, k=10)
        ws = _rank_pos(ranked, "web_search")
        fj = _rank_pos(ranked, "fetch_json")
        assert ws < fj, (
            f"web_search (pos {ws}) should rank above fetch_json (pos {fj}) "
            f"for 'search the web for news'; got: {_names(ranked)}"
        )

    def test_web_query_results(self, catalog):
        ranked = rank_tools("search web query results", catalog, k=10)
        ws = _rank_pos(ranked, "web_search")
        fj = _rank_pos(ranked, "fetch_json")
        assert ws < fj, (
            f"web_search (pos {ws}) should rank above fetch_json (pos {fj}) "
            f"for 'search web query results'; got: {_names(ranked)}"
        )


# ---------------------------------------------------------------------------
# snake_case name token matching
# ---------------------------------------------------------------------------

class TestSnakeCaseTokenMatching:
    """Name tokens derived from snake_case must be matchable individually."""

    def test_fetch_token_matches_fetch_json(self, catalog):
        ranked = rank_tools("fetch json api", catalog, k=10)
        assert "fetch_json" in _names(ranked), (
            "fetch_json should appear when querying 'fetch json api'"
        )

    def test_file_token_matches_file_tools(self, catalog):
        ranked = rank_tools("file read path", catalog, k=10)
        assert "file_read" in _names(ranked), (
            "file_read should appear when querying 'file read path'"
        )

    def test_run_token_matches_run_command(self, catalog):
        ranked = rank_tools("run shell command", catalog, k=10)
        assert "run_command" in _names(ranked), (
            "run_command should appear when querying 'run shell command'"
        )

    def test_web_token_matches_web_tools(self, catalog):
        ranked = rank_tools("web page read url", catalog, k=10)
        assert "web_read" in _names(ranked), (
            "web_read should appear when querying 'web page read url'"
        )

    def test_write_token_matches_file_write(self, catalog):
        ranked = rank_tools("write file content", catalog, k=10)
        assert "file_write" in _names(ranked), (
            "file_write should appear when querying 'write file content'"
        )


# ---------------------------------------------------------------------------
# rank_tools — general behaviour
# ---------------------------------------------------------------------------

class TestRankToolsBehaviour:
    def test_returns_at_most_k(self, catalog):
        ranked = rank_tools("anything", catalog, k=5)
        assert len(ranked) <= 5

    def test_returns_full_dicts(self, catalog):
        ranked = rank_tools("fetch json", catalog, k=3)
        for tool in ranked:
            assert "name" in tool

    def test_empty_query_returns_up_to_k(self, catalog):
        ranked = rank_tools("", catalog, k=4)
        assert len(ranked) <= 4

    def test_empty_tools_returns_empty(self):
        ranked = rank_tools("find location", [], k=8)
        assert ranked == []

    def test_missing_fields_handled_gracefully(self):
        tools = [
            {"name": "tool_a"},
            {"description": "some description"},
            {},
            {"name": "tool_b", "description": "b desc", "parameter_names": ["x", "y"]},
        ]
        # Must not raise
        ranked = rank_tools("desc", tools, k=4)
        assert isinstance(ranked, list)

    def test_deterministic_output(self, catalog):
        r1 = rank_tools("find location ip", catalog, k=8)
        r2 = rank_tools("find location ip", catalog, k=8)
        assert _names(r1) == _names(r2)

    def test_ties_broken_by_original_order(self):
        """When two tools have identical scores, earlier one in list wins."""
        tools = [
            {"name": "alpha", "description": "a test tool"},
            {"name": "beta", "description": "a test tool"},
        ]
        ranked = rank_tools("test", tools, k=2)
        assert _names(ranked)[0] == "alpha", "tie should preserve original order"

    def test_name_weight_dominates_description(self):
        """A strong name match should beat a weaker description-only match."""
        tools = [
            {"name": "json_parser", "description": "parse structured data"},
            {"name": "data_loader", "description": "load json content from files"},
        ]
        ranked = rank_tools("json", tools, k=2)
        assert _names(ranked)[0] == "json_parser", (
            "name match (weight ×4) should beat description-only match (weight ×2)"
        )


# ---------------------------------------------------------------------------
# ensure_core
# ---------------------------------------------------------------------------

class TestEnsureCore:
    def test_core_tools_always_included(self, catalog):
        """Core tools present in the catalog must appear in ensure_core output."""
        # Rank on something unrelated so core tools may not make top-3
        ranked = rank_tools("word document excel spreadsheet", catalog, k=3)
        result = ensure_core(ranked, catalog)
        result_names = set(_names(result))
        catalog_names = {t["name"] for t in catalog}
        for core_name in ALWAYS_AVAILABLE:
            if core_name in catalog_names:
                assert core_name in result_names, (
                    f"Core tool '{core_name}' should always appear in ensure_core output"
                )

    def test_no_duplicates_in_ensure_core(self, catalog):
        ranked = rank_tools("fetch json location", catalog, k=8)
        result = ensure_core(ranked, catalog)
        names = _names(result)
        assert len(names) == len(set(names)), "ensure_core must not introduce duplicates"

    def test_ranked_order_preserved_at_front(self, catalog):
        ranked = rank_tools("file read write", catalog, k=5)
        result = ensure_core(ranked, catalog)
        # The first len(ranked) slots of result must match ranked (in order)
        assert result[:len(ranked)] == ranked

    def test_empty_ranked_still_adds_core(self, catalog):
        result = ensure_core([], catalog)
        result_names = set(_names(result))
        catalog_names = {t["name"] for t in catalog}
        for core_name in ALWAYS_AVAILABLE:
            if core_name in catalog_names:
                assert core_name in result_names

    def test_always_available_frozenset_contents(self):
        assert "fetch_json" in ALWAYS_AVAILABLE
        assert "web_search" in ALWAYS_AVAILABLE
        assert "file_read" in ALWAYS_AVAILABLE
        assert "file_write" in ALWAYS_AVAILABLE
        assert "run_command" in ALWAYS_AVAILABLE

    def test_core_tools_not_in_catalog_silently_skipped(self):
        """If a core tool is absent from the provided catalog, no error."""
        mini_catalog = [{"name": "fetch_json", "description": "call an API"}]
        result = ensure_core([], mini_catalog)
        assert _names(result) == ["fetch_json"]
