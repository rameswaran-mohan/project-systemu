"""Slice 4 — pure filter for the Tools registry toolbar (board 5a: ⌕ search + status ▾)."""
from systemu.interface.pages.tools import _filter_tools


def _t(name, status, desc=""):
    return {"name": name, "status": status, "description": desc}


TOOLS = [
    _t("web_search", "deployed", "search the web"),
    _t("csv_clean", "proposed", "clean csv files"),
    _t("pdf_extract", "deployed", "extract pdf tables"),
]


class TestFilterTools:
    def test_empty_passthrough(self):
        assert _filter_tools(TOOLS, "", "all") == TOOLS
        assert _filter_tools(TOOLS, None, None) == TOOLS

    def test_query_matches_name_case_insensitive(self):
        assert [t["name"] for t in _filter_tools(TOOLS, "CSV", "all")] == ["csv_clean"]

    def test_query_matches_description(self):
        assert [t["name"] for t in _filter_tools(TOOLS, "tables", "all")] == ["pdf_extract"]

    def test_status_filter_exact(self):
        assert [t["name"] for t in _filter_tools(TOOLS, "", "proposed")] == ["csv_clean"]

    def test_status_case_insensitive(self):
        assert len(_filter_tools(TOOLS, "", "DEPLOYED")) == 2

    def test_query_and_status_combine(self):
        assert _filter_tools(TOOLS, "web", "proposed") == []
        assert [t["name"] for t in _filter_tools(TOOLS, "web", "deployed")] == ["web_search"]

    def test_no_match_yields_empty(self):
        assert _filter_tools(TOOLS, "zzz-nothing", "all") == []

    def test_missing_keys_are_safe(self):
        # a tool dict missing status/description must not crash the filter
        assert _filter_tools([{"name": "x"}], "x", "all") == [{"name": "x"}]
        assert _filter_tools([{"name": "x"}], "", "deployed") == []
