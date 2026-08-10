"""Task 6 - the guided tour walks the rooms in the operator's persona order.

The six steps never change: only their ORDER does, so an Enterprise operator
meets Inbox (control) second while a Personal operator meets Build (forge)
third. ?tour=N indexes the persona-ordered list, which is derived server-side
from a stored fact - links stay stateless and deterministic.

Every persona order must be a PERMUTATION of the six steps: a skin may
re-emphasise, never hide a room (persona changes emphasis, not capability).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(str(tmp_path / "vault"))


def _with_persona(vault: Vault, persona: str) -> Vault:
    from systemu.runtime.user_profile import add_fact
    add_fact(vault, "Usage persona: " + persona, source="onboarding",
             tags=["persona"])
    return vault


def _fake_state(monkeypatch, vault_obj) -> None:
    """Point AppState.get().vault at our fixture vault (no browser needed)."""
    from systemu.interface import dashboard_state

    class _State:
        vault = vault_obj

        @classmethod
        def get(cls):
            return cls

    monkeypatch.setattr(dashboard_state, "AppState", _State)


class TestPersonaOrder:
    def test_tour_order_is_a_permutation_for_every_persona(self):
        from systemu.interface.tour import TOUR_STEPS, tour_steps_for
        from systemu.interface.pages.welcome import personas
        base_titles = {s["title"] for s in TOUR_STEPS}
        for p in list(personas()) + [None, ""]:
            steps = tour_steps_for(p)
            assert {s["title"] for s in steps} == base_titles
            assert len(steps) == len(TOUR_STEPS) == 6

    def test_enterprise_tour_reaches_inbox_second(self):
        from systemu.interface.tour import tour_steps_for
        assert tour_steps_for("Enterprise professional")[1]["route"] == "/inbox"

    def test_personal_tour_reaches_build_third(self):
        """Personal's aha is the forge - Build lands before Work/Inbox."""
        from systemu.interface.tour import tour_steps_for
        assert tour_steps_for("Personal")[2]["route"] == "/tools"

    def test_solo_business_reaches_the_work_board_second(self):
        from systemu.interface.tour import tour_steps_for
        assert tour_steps_for("Solo business")[1]["route"] == "/work"

    def test_every_order_starts_at_home(self):
        """Step 1 is always Home: the tour begins where the wizard lands."""
        from systemu.interface.tour import tour_steps_for
        from systemu.interface.persona_content import PERSONA_CONTENT
        for p in list(PERSONA_CONTENT) + [None]:
            assert tour_steps_for(p)[0]["route"] == "/"

    def test_unknown_persona_falls_back_to_the_default_order(self):
        from systemu.interface.tour import TOUR_STEPS, tour_steps_for
        for p in (None, "", "   ", "Not A Persona"):
            assert tour_steps_for(p) == list(TOUR_STEPS)

    def test_a_broken_order_falls_back_rather_than_dropping_a_room(
            self, monkeypatch):
        """A skin that names a bad index must not strand a room."""
        from systemu.interface import persona_content
        from systemu.interface.tour import TOUR_STEPS, tour_steps_for
        broken = ("nonsense", [], [0, 1, 99], [0, 1, 2, 3, 4, -1])
        for order in broken:
            monkeypatch.setattr(
                persona_content, "skin_for",
                lambda _p, _o=order: type("S", (), {"tour_order": _o})())
            assert tour_steps_for("Personal") == list(TOUR_STEPS)

    def test_tour_step_keeps_its_bounds_safe_contract(self):
        from systemu.interface.tour import TOUR_STEPS, tour_step
        assert tour_step(0) == TOUR_STEPS[0]
        assert tour_step(len(TOUR_STEPS)) is None
        assert tour_step(-1) is None


class TestRenderTimeResolution:
    def test_render_resolves_the_stored_persona(self, vault, monkeypatch):
        from systemu.interface import tour
        _fake_state(monkeypatch, _with_persona(vault, "Enterprise professional"))
        assert tour._persona_steps()[1]["route"] == "/inbox"

    def test_no_persona_fact_means_the_default_order(self, vault, monkeypatch):
        from systemu.interface import tour
        _fake_state(monkeypatch, vault)
        assert tour._persona_steps() == list(tour.TOUR_STEPS)

    def test_missing_vault_means_the_default_order(self, monkeypatch):
        from systemu.interface import tour
        _fake_state(monkeypatch, None)
        assert tour._persona_steps() == list(tour.TOUR_STEPS)

    def test_app_state_failure_means_the_default_order(self, monkeypatch):
        """Defensive: the tour must render even with no running app."""
        from systemu.interface import dashboard_state, tour

        class _Boom:
            @classmethod
            def get(cls):
                raise RuntimeError("no app state")

        monkeypatch.setattr(dashboard_state, "AppState", _Boom)
        assert tour._persona_steps() == list(tour.TOUR_STEPS)

    def test_a_hostile_vault_means_the_default_order(self, monkeypatch):
        from systemu.interface import tour
        _fake_state(monkeypatch, object())
        assert tour._persona_steps() == list(tour.TOUR_STEPS)


class TestWiring:
    """Reachability pins: reverting the wiring must turn a NAMED test red."""

    def test_card_navigates_within_the_persona_list(self):
        """Back/Next must index the persona order, not the raw TOUR_STEPS."""
        from systemu.interface import tour
        src = inspect.getsource(tour.render_tour_card)
        assert "TOUR_STEPS[" not in src, \
            "Back/Next closures must index the persona-ordered list"
        assert "steps[" in src

    def test_card_accepts_a_resolved_step_list(self):
        from systemu.interface.tour import render_tour_card
        assert "steps" in inspect.signature(render_tour_card).parameters

    def test_maybe_render_tour_resolves_the_persona_once(self):
        from systemu.interface import tour
        src = inspect.getsource(tour.maybe_render_tour)
        assert "_persona_steps" in src, \
            "?tour=N must index the persona-ordered list"

    def test_completion_paths_are_untouched(self):
        from systemu.interface import tour
        src = inspect.getsource(tour.render_tour_card)
        assert "_complete(False)" in src and "_complete(True)" in src
