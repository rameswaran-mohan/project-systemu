"""Phase 5 — the 6-spine command-center nav (Slice 2a shape).

THE nav canary. Slice 1 established the 6 flat spines; Slice 2a repoints
the Work spine at the new workflow-centric ``/work`` page (was /scrolls).
Covers:
  - the 6 flat spines (order, paths — Work → /work, no duplicates,
    Inbox demoted);
  - every folded sub-route (/scrolls, /activities, /workflow/{id}, /chat,
    /skills, …) maps to its spine via ``spine_of``;
  - ``active_nav_path`` delegates to the spine model;
  - folded routes stay REGISTERED (/scrolls + /activities keep rendering —
    the nav repoints in 2a; full redirects land in a later slice) and the
    new /work route IS registered;
  - legacy URLs stay out of the sidebar; merged-page builders importable
    (carried forward from the v0.7.2 canary — still-relevant invariants).
"""
import inspect

from systemu.interface.dashboard import NAV_SPINES, NAV_ITEMS, active_nav_path, spine_of


def test_six_spines_in_order():
    labels = [label for _p, _i, label in NAV_SPINES]
    assert labels == ["Home", "Work", "Shadows", "Build", "Insights", "Settings"]


def test_nav_items_is_the_six_spines():
    paths = [p for p, _i, _l in NAV_ITEMS]
    assert paths == ["/", "/work", "/army", "/tools", "/insights", "/settings"]
    assert "/inbox" not in paths          # demoted to the right rail + page
    assert "/scrolls" not in paths        # Slice 2a: Work spine → /work


def test_sub_routes_map_to_their_spine():
    assert spine_of("/work") == "/work"              # Work (identity)
    assert spine_of("/scrolls") == "/work"           # Work (folded, 2a)
    assert spine_of("/activities") == "/work"        # Work (folded, 2a)
    assert spine_of("/workflow/wf_1") == "/work"     # Work (deep)
    assert spine_of("/chat") == "/work"              # Work (task creation)
    assert spine_of("/skills") == "/tools"           # Build
    assert spine_of("/evolutions") == "/tools"       # Build
    assert spine_of("/workshop") == "/tools"         # Build
    assert spine_of("/memory/sh_1") == "/army"       # Shadows (deep)


def test_active_nav_path_resolves_to_spine():
    nav = [p for p, _i, _l in NAV_ITEMS]
    assert active_nav_path("/work", nav) == "/work"
    assert active_nav_path("/scrolls", nav) == "/work"
    assert active_nav_path("/activities", nav) == "/work"
    assert active_nav_path("/skills", nav) == "/tools"
    assert active_nav_path("/inbox", nav) == ""      # no spine (right rail)
    assert active_nav_path("/", nav) == "/"


def test_nav_no_duplicate_paths():
    paths = [p for p, _i, _l in NAV_SPINES]
    assert len(paths) == len(set(paths)), "duplicate path in NAV_SPINES"


# ── Slice 2a contract: repoint the nav WITHOUT deleting routes ───────────────

def test_folded_routes_still_registered():
    """/work must be registered, and /scrolls,/activities,/skills,/workshop,
    /evolutions,/chat,/inbox must all stay reachable by URL — the nav
    repoints, the routes live on.  (Full redirects land in later slices.)"""
    from systemu.interface import dashboard

    src = inspect.getsource(dashboard.register_routes)
    for route in ("/work", "/scrolls", "/activities", "/skills", "/workshop",
                  "/evolutions", "/chat", "/inbox"):
        assert f'@ui.page("{route}")' in src, f"{route} route not registered"


def test_folded_routes_not_in_sidebar_but_spine_mapped():
    paths = [p for p, _i, _l in NAV_ITEMS]
    for folded in ("/scrolls", "/chat", "/activities", "/skills", "/workshop",
                   "/evolutions"):
        assert folded not in paths
        assert spine_of(folded) != "", f"{folded} lost its spine mapping"


def test_work_page_builder_importable():
    from systemu.interface.pages.work import build_work_page

    assert callable(build_work_page)


# ── Carried forward from the v0.7.2 canary (still-relevant invariants) ──────

def test_legacy_paths_not_in_sidebar():
    """Legacy/merged URLs must redirect, never reappear as nav entries."""
    paths = [p for p, _i, _l in NAV_ITEMS]
    labels = [label for _p, _i, label in NAV_ITEMS]
    for legacy in ("/systemu-chat", "/memory", "/flywheel", "/notifications",
                   "/shadows"):
        assert legacy not in paths
    assert "Systemu Chat" not in labels


def test_insights_page_importable():
    from systemu.interface.pages.insights import build_insights_page, _VALID_TABS

    assert callable(build_insights_page)
    # Slice 4d: the "actions" tab is gone — decisions live ONLY in the Inbox.
    assert set(_VALID_TABS) == {"memory", "flywheel", "events"}


def test_chat_tabs_importable():
    from systemu.interface.pages.chat_page import (
        build_chat_tabs,
        _VALID_CHAT_TABS,
    )

    assert callable(build_chat_tabs)
    assert set(_VALID_CHAT_TABS) == {"compose", "live"}


def test_register_routes_callable_imports_cleanly():
    """The dashboard module must still register routes without exploding —
    catches missing imports in the route-registration import block. We don't
    call register_routes() (needs a NiceGUI app + AppState); importing the
    symbol is sufficient."""
    from systemu.interface import dashboard

    assert callable(dashboard.register_routes)
