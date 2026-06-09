"""Phase 4 Task 1 — exact, segment-aware active-route detection.

Replaces the old char-prefix `startswith` (which false-positives
/toolsmith -> /tools and cannot map deep detail pages to a spine parent).
"""
from systemu.interface.dashboard import active_nav_path


PATHS = ["/", "/chat", "/scrolls", "/army", "/activities",
         "/tools", "/skills", "/workshop", "/evolutions",
         "/inbox", "/insights", "/settings"]


def test_exact_match_wins():
    assert active_nav_path("/inbox", PATHS) == "/inbox"
    assert active_nav_path("/settings", PATHS) == "/settings"


def test_root_only_matches_root():
    assert active_nav_path("/", PATHS) == "/"


def test_segment_child_highlights_its_root():
    # A deep page under a real nav root highlights that root.
    assert active_nav_path("/scrolls/abc123", PATHS) == "/scrolls"
    assert active_nav_path("/tools/some_tool", PATHS) == "/tools"


def test_deep_detail_page_maps_to_spine_parent():
    # /workflow/{id} and /memory/{id} have no nav entry of their own.
    assert active_nav_path("/workflow/wf_42", PATHS) == "/activities"
    assert active_nav_path("/memory/shadow_7", PATHS) == "/army"


def test_no_char_prefix_false_positive():
    # /toolsmith must NOT light up /tools (the bug in the old startswith).
    assert active_nav_path("/toolsmith", PATHS) == ""


def test_unknown_route_highlights_nothing():
    assert active_nav_path("/nope/here", PATHS) == ""
