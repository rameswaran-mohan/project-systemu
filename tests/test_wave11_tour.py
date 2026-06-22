"""W11.5 — the guided tour: mandatory on first run, replayable forever.

Operator requirement (2026-06-12): "a mandatory tutorial or something at
the very first start of launch just after installation" + "reduce the
learning curve".

Shape: the wizard's Finish lands on /?tour=0; a floating card walks the
six spine surfaces in plain language, navigating route to route. The tour
never redirects (it IS navigation) — un-finished tours surface a header
"Take the tour" pill until completed. "End tour" also completes (no
hostage UX); Settings offers a replay.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(str(tmp_path / "vault"))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SYSTEMU_SKIP_ONBOARDING", raising=False)


def _finished_wizard(vault):
    from systemu.interface.pages.welcome import save_onboarding
    save_onboarding(vault, name="R", location="X", timezone="UTC",
                    output_dir="C:/x")


class TestTourSteps:
    def test_steps_cover_the_spines_in_plain_language(self):
        from systemu.interface.tour import TOUR_STEPS
        assert len(TOUR_STEPS) >= 5
        for step in TOUR_STEPS:
            assert step["route"].startswith("/")
            assert step["title"].strip() and step["body"].strip()
            # plain language: every body explains, never just names the lore
            assert len(step["body"]) > 40
        routes = [s["route"] for s in TOUR_STEPS]
        for must in ("/", "/chat", "/work", "/inbox", "/settings"):
            assert must in routes

    def test_steps_routes_are_registered_pages(self):
        """A renamed route must fail the suite, not strand the tour."""
        from systemu.interface import tour, dashboard
        src = inspect.getsource(dashboard)
        for step in tour.TOUR_STEPS:
            assert f'@ui.page("{step["route"]}")' in src, \
                f"tour step points at unregistered route {step['route']}"

    def test_tour_step_bounds(self):
        from systemu.interface.tour import TOUR_STEPS, tour_step
        assert tour_step(0) == TOUR_STEPS[0]
        assert tour_step(len(TOUR_STEPS)) is None
        assert tour_step(-1) is None


class TestTourState:
    def test_not_pending_before_wizard(self, vault):
        """Pre-wizard installs are the GATE's job — the pill would nag the
        wrong moment."""
        from systemu.interface.tour import is_tour_pending
        assert is_tour_pending(vault) is False

    def test_pending_after_wizard_until_completed(self, vault):
        from systemu.interface.tour import is_tour_pending, mark_tour_completed
        _finished_wizard(vault)
        assert is_tour_pending(vault) is True
        mark_tour_completed(vault)
        assert is_tour_pending(vault) is False

    def test_ending_early_also_completes(self, vault):
        """'End tour' counts as done — mandatory must never mean hostage."""
        from systemu.interface.tour import is_tour_pending, mark_tour_completed
        _finished_wizard(vault)
        mark_tour_completed(vault, ended_early=True)
        assert is_tour_pending(vault) is False

    def test_env_escape_hatch(self, vault, monkeypatch):
        from systemu.interface.tour import is_tour_pending
        _finished_wizard(vault)
        monkeypatch.setenv("SYSTEMU_SKIP_ONBOARDING", "1")
        assert is_tour_pending(vault) is False

    def test_never_raises(self):
        from systemu.interface.tour import is_tour_pending
        assert is_tour_pending(object()) is False

    def test_completion_feeds_first_run_check(self, vault):
        from systemu.interface.tour import mark_tour_completed
        from systemu.runtime.first_run import tour_completed
        assert tour_completed(vault) is False
        mark_tour_completed(vault)
        assert tour_completed(vault) is True


class TestWiring:
    def test_layout_renders_tour_card_and_pill(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard)
        assert "maybe_render_tour" in src, \
            "?tour=N must render the floating card on every layout"
        assert "render_tour_pill" in src, \
            "unfinished tours must stay visible (header pill) until done"

    def test_settings_offers_replay(self):
        from systemu.interface.pages import settings
        src = inspect.getsource(settings)
        assert "tour=0" in src, "Settings must offer 'Replay the tour'"

    def test_card_completion_paths_write_the_fact(self):
        from systemu.interface import tour
        src = inspect.getsource(tour.render_tour_card)
        assert "mark_tour_completed" in src
        assert "_complete(False)" in src, "Finish must record completion"
        assert "_complete(True)" in src, \
            "End tour must record completion too (no hostage UX)"
