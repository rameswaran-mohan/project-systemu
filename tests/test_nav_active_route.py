"""Phase 4 Task 1 — exact, segment-aware active-route detection.

Replaces the old char-prefix `startswith` (which false-positives
/toolsmith -> /tools and cannot map deep detail pages to a spine parent).

Phase 5 Slice 1: `active_nav_path` delegates to the 6-spine model
(`spine_of`) — the nav list is the 6 spines and every folded sub-route
(/activities, /skills, /workflow/{id}, …) highlights its OWNING SPINE, not a
same-named nav entry.

Phase 5 Slice 2a: the Work spine is now `/work` (workflow-centric list) —
/scrolls, /activities, /workflow/{id}, and /chat all fold into it.
Assertions updated accordingly:
  * PATHS is the 6-spine nav with /work (the only list the sidebar renders);
  * /scrolls, /activities, /chat, /workflow/{id} → /work;
  * /inbox → "" (demoted from the left nav — the right rail owns it).
The no-false-positive and unknown-route guarantees are unchanged.
"""
from systemu.interface.dashboard import active_nav_path


PATHS = ["/", "/work", "/shadows", "/tools", "/insights", "/settings"]


def test_exact_match_wins():
    assert active_nav_path("/work", PATHS) == "/work"
    assert active_nav_path("/settings", PATHS) == "/settings"


def test_root_only_matches_root():
    assert active_nav_path("/", PATHS) == "/"


def test_segment_child_highlights_its_root():
    # A deep page under a real nav root highlights that root.
    assert active_nav_path("/work/abc123", PATHS) == "/work"
    assert active_nav_path("/tools/some_tool", PATHS) == "/tools"


def test_deep_detail_page_maps_to_spine_parent():
    # /workflow/{id} and /memory/{id} have no nav entry of their own.
    # Phase 5 Slice 2a: workflows belong to the Work spine (/work).
    assert active_nav_path("/workflow/wf_42", PATHS) == "/work"
    assert active_nav_path("/memory/shadow_7", PATHS) == "/shadows"


def test_folded_sub_route_highlights_its_spine():
    # Phase 5: routes demoted from the nav still light their owning spine.
    # Slice 2a folds /scrolls itself (the old Work primary) into /work.
    assert active_nav_path("/scrolls", PATHS) == "/work"
    assert active_nav_path("/scrolls/abc123", PATHS) == "/work"
    assert active_nav_path("/activities", PATHS) == "/work"
    assert active_nav_path("/chat", PATHS) == "/work"
    assert active_nav_path("/skills", PATHS) == "/tools"


def test_inbox_highlights_nothing():
    # Phase 5: Inbox is demoted from the left nav (right rail + /inbox page).
    assert active_nav_path("/inbox", PATHS) == ""


def test_no_char_prefix_false_positive():
    # /toolsmith must NOT light up /tools (the bug in the old startswith).
    assert active_nav_path("/toolsmith", PATHS) == ""


def test_unknown_route_highlights_nothing():
    assert active_nav_path("/nope/here", PATHS) == ""
