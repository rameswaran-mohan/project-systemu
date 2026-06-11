"""W4.1 — the shared listing-page filter (search + one select dimension).

One pure filter behind scrolls / activities / shadows / skills toolbars. These
pin its semantics so the four pages stay consistent and the logic is covered
without driving NiceGUI.
"""
from __future__ import annotations

from systemu.interface.components.list_filter import filter_rows, select_options


ROWS = [
    {"id": "1", "name": "Refine invoices", "description": "monthly billing",
     "status": "pending_approval", "tags": ["finance", "ocr"]},
    {"id": "2", "name": "Scrape news", "description": "RSS crawler",
     "status": "approved", "tags": ["web"]},
    {"id": "3", "name": "Email digest", "description": "summarise inbox",
     "status": "approved", "tags": []},
    {"id": "4"},  # deliberately missing every optional key
]


class TestFilterRows:
    def test_no_filters_returns_all_in_order(self):
        assert [r["id"] for r in filter_rows(ROWS)] == ["1", "2", "3", "4"]

    def test_query_matches_name_case_insensitive(self):
        out = filter_rows(ROWS, query="EMAIL")
        assert [r["id"] for r in out] == ["3"]

    def test_query_matches_secondary_search_key(self):
        out = filter_rows(ROWS, query="crawler", search_keys=("name", "description"))
        assert [r["id"] for r in out] == ["2"]

    def test_query_matches_list_field(self):
        out = filter_rows(ROWS, query="finance", list_search_keys=("tags",))
        assert [r["id"] for r in out] == ["1"]

    def test_select_value_exact_status_match(self):
        out = filter_rows(ROWS, select_value="approved")
        assert [r["id"] for r in out] == ["2", "3"]

    def test_select_value_case_insensitive(self):
        out = filter_rows(ROWS, select_value="APPROVED")
        assert [r["id"] for r in out] == ["2", "3"]

    def test_all_and_empty_select_disable_the_dimension(self):
        assert len(filter_rows(ROWS, select_value="all")) == 4
        assert len(filter_rows(ROWS, select_value="")) == 4

    def test_query_and_select_are_anded(self):
        out = filter_rows(ROWS, query="news", select_value="approved")
        assert [r["id"] for r in out] == ["2"]
        # name matches but status doesn't → excluded
        assert filter_rows(ROWS, query="invoices", select_value="approved") == []

    def test_missing_keys_do_not_raise(self):
        # Row 4 has no name/status/tags — must be tolerated, not crash.
        assert filter_rows(ROWS, query="zzz") == []
        assert filter_rows(ROWS, select_value="approved", query="") == ROWS[1:3]

    def test_serves_a_category_dimension_too(self):
        rows = [{"id": "a", "name": "x", "category": "browser"},
                {"id": "b", "name": "y", "category": "data"}]
        out = filter_rows(rows, select_value="data", select_key="category")
        assert [r["id"] for r in out] == ["b"]


class TestSelectOptions:
    def test_sorted_unique_with_all_prepended(self):
        assert select_options(ROWS) == ["all", "approved", "pending_approval"]

    def test_without_all(self):
        assert select_options(ROWS, prepend_all=False) == ["approved", "pending_approval"]

    def test_custom_key(self):
        rows = [{"category": "b"}, {"category": "a"}, {"category": "a"}, {}]
        assert select_options(rows, key="category") == ["all", "a", "b"]
