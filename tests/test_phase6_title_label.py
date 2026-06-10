"""Phase 6a — page titles align with their nav labels (P8).

The header title rendered by ``_build_layout("<title>", path)`` for each of
the 6 spine routes should match that spine's NAV_SPINES label, so the page
the user lands on names itself the same way the sidebar item that took them
there does.  Two known mismatches motivated this fix:

  - ``/``     title was "🖥️ Console"      (label "Home")  → "🏠 Home"
  - ``/tools`` title was "🔧 Tool Registry" (label "Build") → "🔧 Build"

Deep/sub routes (/scrolls, /workflow/{id}, /memory/{id}, …) are intentionally
NOT covered — they fold into a spine but legitimately carry their own page
titles.  Only the 6 top-level spine routes are asserted here.
"""
import inspect
import re

from systemu.interface import dashboard
from systemu.interface.dashboard import NAV_SPINES


def _strip(title: str) -> str:
    """Drop leading emoji/symbols + whitespace, keep the word(s)."""
    # Keep ASCII letters/digits/space; collapse leftover whitespace.
    return re.sub(r"[^A-Za-z0-9 ]+", "", title).strip()


def _spine_titles_from_source() -> dict[str, str]:
    """Map spine path -> the literal _build_layout title used for it.

    Parses register_routes() source for ``_build_layout("<title>", "<path>")``
    calls with *literal* string args (the spine routes all use literals;
    f-string deep routes are skipped by the non-greedy literal pattern).
    """
    src = inspect.getsource(dashboard.register_routes)
    pat = re.compile(r'_build_layout\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)')
    return {path: title for title, path in pat.findall(src)}


def test_spine_titles_match_nav_labels():
    titles = _spine_titles_from_source()
    spine_paths = {p for p, _i, _l in NAV_SPINES}
    # Sanity: every spine route must have a parseable literal-title layout call.
    missing = spine_paths - set(titles)
    assert not missing, f"no _build_layout literal title found for spines: {missing}"

    for path, _icon, label in NAV_SPINES:
        got = _strip(titles[path])
        assert label.lower() in got.lower(), (
            f"spine {path!r}: title {titles[path]!r} (->{got!r}) "
            f"should contain nav label {label!r}"
        )


def test_home_and_build_titles_specifically():
    titles = _spine_titles_from_source()
    assert _strip(titles["/"]) == "Home"
    assert _strip(titles["/tools"]) == "Build"
