"""W4.2 — Insights deep-link round-trip.

The load-direction (``/insights?tab=…`` selects the tab) was already wired and
is covered by tests/test_insights_pending_decisions.py. W4.2 adds the
click-direction: selecting a tab rewrites the URL via history.replaceState so
the active tab is shareable. The URL format lives in one pure helper so both
directions agree with the redirect targets in dashboard.py.
"""
from __future__ import annotations

from systemu.interface.pages.insights import _tab_url, _VALID_TABS


class TestTabUrl:
    def test_format_matches_query_param_contract(self):
        assert _tab_url("flywheel") == "/insights?tab=flywheel"
        assert _tab_url("memory") == "/insights?tab=memory"
        assert _tab_url("events") == "/insights?tab=events"

    def test_round_trips_with_resolve_tab(self):
        # Every valid tab's URL must carry the same tab token back.
        from systemu.interface.pages.insights import _resolve_tab
        for tab in _VALID_TABS:
            url = _tab_url(tab)
            assert url.endswith(f"tab={tab}")
            assert _resolve_tab(tab) == tab

    def test_matches_dashboard_redirect_targets(self):
        # The legacy-route redirects in dashboard.py point at these exact URLs;
        # keep them in lock-step so a deep link and a click produce one format.
        from systemu.interface.dashboard import _legacy_redirect_routes
        targets = {t for _, t in _legacy_redirect_routes()}
        assert _tab_url("memory") in targets
        assert _tab_url("flywheel") in targets
        assert _tab_url("events") in targets
