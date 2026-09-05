"""Phase 2e - per-page micro-tours (?ptour=N).

The main tour (?tour=N) walks the six spine rooms once and records completion.
A PAGE tour is the opposite kind of object: two optional hints about the room
you are already standing in, reached from a small "?" pill, never auto-fired,
and deliberately stateless - no completion fact, replayable forever.

These tests pin the properties that make that safe:
  * the hint lookup is pure, bounds-safe and never raises;
  * a page with no hints for the operator's persona shows no pill;
  * without ?ptour in the query string NOTHING renders (never auto-fires);
  * the layout actually consults both hooks (AST reachability pins);
  * the new copy passes the same honesty wall the rest of the skins pass.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


# ---------------------------------------------------------------------------
# The hint registry (persona_content.PersonaSkin.page_hints)
# ---------------------------------------------------------------------------
class TestHintRegistry:
    def test_skin_carries_page_hints_defaulting_to_empty(self):
        from systemu.interface.persona_content import PersonaSkin
        bare = PersonaSkin(starters=["a"], dare_line="d", empty_work="w",
                           empty_shadows="s")
        assert bare.page_hints == {}

    def test_default_skin_has_hints_for_work_and_tools(self):
        from systemu.interface.persona_content import DEFAULT_SKIN
        for route in ("/work", "/tools"):
            assert DEFAULT_SKIN.page_hints.get(route), \
                f"DEFAULT_SKIN must carry hints for {route}"

    def test_personal_owns_the_forge_angle_on_tools(self):
        from systemu.interface.persona_content import PERSONA_CONTENT
        assert PERSONA_CONTENT["Personal"].page_hints.get("/tools")

    def test_enterprise_owns_the_audit_angle_on_insights(self):
        from systemu.interface.persona_content import PERSONA_CONTENT
        assert PERSONA_CONTENT["Enterprise professional"].page_hints.get(
            "/insights")

    def test_every_hint_route_is_a_declared_page_tour_route(self):
        from systemu.interface.persona_content import (
            DEFAULT_SKIN, PERSONA_CONTENT,
        )
        from systemu.interface.tour import PAGE_TOUR_ROUTES
        for skin in list(PERSONA_CONTENT.values()) + [DEFAULT_SKIN]:
            for route in skin.page_hints:
                assert route in PAGE_TOUR_ROUTES, \
                    f"{route} is not a page-tour route"

    def test_home_never_carries_page_hints(self):
        """Home belongs to the MAIN tour - a second card there is noise."""
        from systemu.interface.persona_content import (
            DEFAULT_SKIN, PERSONA_CONTENT,
        )
        from systemu.interface.tour import PAGE_TOUR_ROUTES
        assert "/" not in PAGE_TOUR_ROUTES
        for skin in list(PERSONA_CONTENT.values()) + [DEFAULT_SKIN]:
            assert "/" not in skin.page_hints

    def test_page_tour_routes_are_registered_pages(self):
        """A renamed route must fail the suite, not strand a pill."""
        from systemu.interface import dashboard
        from systemu.interface.tour import PAGE_TOUR_ROUTES
        src = inspect.getsource(dashboard)
        for route in PAGE_TOUR_ROUTES:
            assert f'@ui.page("{route}")' in src, \
                f"page-tour route {route} is not a registered page"

    def test_every_hint_is_one_or_two_well_formed_steps(self):
        from systemu.interface.persona_content import (
            DEFAULT_SKIN, PERSONA_CONTENT,
        )
        for skin in list(PERSONA_CONTENT.values()) + [DEFAULT_SKIN]:
            for route, steps in skin.page_hints.items():
                assert 1 <= len(steps) <= 2, \
                    f"{route}: a micro-tour is 1-2 steps, got {len(steps)}"
                for step in steps:
                    assert step["title"].strip()
                    # plain language: a hint explains, it never just names
                    assert len(step["body"]) > 40


# ---------------------------------------------------------------------------
# page_tour_steps(route, persona)
# ---------------------------------------------------------------------------
class TestPageTourSteps:
    def test_known_route_default_persona(self):
        from systemu.interface.persona_content import DEFAULT_SKIN
        from systemu.interface.tour import page_tour_steps
        assert page_tour_steps("/work", None) == DEFAULT_SKIN.page_hints["/work"]

    def test_unknown_route_is_empty(self):
        from systemu.interface.tour import page_tour_steps
        assert page_tour_steps("/nope", None) == []
        assert page_tour_steps("/", None) == []
        assert page_tour_steps("", None) == []

    def test_unknown_persona_falls_back_to_the_default_skin(self):
        from systemu.interface.persona_content import DEFAULT_SKIN
        from systemu.interface.tour import page_tour_steps
        for persona in (None, "", "   ", "Martian"):
            assert page_tour_steps("/tools", persona) == \
                DEFAULT_SKIN.page_hints["/tools"]

    def test_a_persona_hint_wins_over_the_default(self):
        from systemu.interface.persona_content import (
            DEFAULT_SKIN, PERSONA_CONTENT,
        )
        from systemu.interface.tour import page_tour_steps
        got = page_tour_steps("/tools", "Personal")
        assert got == PERSONA_CONTENT["Personal"].page_hints["/tools"]
        assert got != DEFAULT_SKIN.page_hints["/tools"]

    def test_a_persona_without_hints_for_the_route_falls_back(self):
        """A skin re-emphasises; it never strands a room that HAS hints."""
        from systemu.interface.persona_content import DEFAULT_SKIN
        from systemu.interface.tour import page_tour_steps
        assert page_tour_steps("/work", "Personal") == \
            DEFAULT_SKIN.page_hints["/work"]

    def test_returns_a_copy_so_a_caller_cannot_edit_the_registry(self):
        from systemu.interface.persona_content import DEFAULT_SKIN
        from systemu.interface.tour import page_tour_steps
        before = len(DEFAULT_SKIN.page_hints["/work"])
        page_tour_steps("/work", None).append({"title": "x", "body": "y"})
        assert len(DEFAULT_SKIN.page_hints["/work"]) == before

    def test_never_raises_on_hostile_input(self):
        from systemu.interface.tour import page_tour_steps
        for route in (None, 7, object(), b"/work"):
            for persona in (None, 7, object()):
                assert page_tour_steps(route, persona) == []

    def test_a_broken_skin_falls_back_to_the_default(self, monkeypatch):
        """A malformed skin must not strand a page that HAS hints."""
        from systemu.interface import persona_content
        from systemu.interface.persona_content import DEFAULT_SKIN
        from systemu.interface.tour import page_tour_steps
        for broken in ("nonsense", None, {"/work": "not a list"},
                       {"/work": []}):
            monkeypatch.setattr(
                persona_content, "skin_for",
                lambda _p, _h=broken: type("S", (), {"page_hints": _h})())
            assert page_tour_steps("/work", "Personal") == \
                DEFAULT_SKIN.page_hints["/work"]

    def test_a_skin_lookup_that_raises_falls_back_to_the_default(
            self, monkeypatch):
        from systemu.interface import persona_content
        from systemu.interface.persona_content import DEFAULT_SKIN
        from systemu.interface.tour import page_tour_steps

        def _boom(_p):
            raise RuntimeError("hostile skin")

        monkeypatch.setattr(persona_content, "skin_for", _boom)
        assert page_tour_steps("/work", "Personal") == \
            DEFAULT_SKIN.page_hints["/work"]


# ---------------------------------------------------------------------------
# ?ptour=N parsing - bounds-safe, and it NEVER auto-fires
# ---------------------------------------------------------------------------
class TestParamParsing:
    def test_parse_step_param_accepts_only_plain_indices(self):
        from systemu.interface.tour import parse_step_param
        assert parse_step_param("0") == 0
        assert parse_step_param("12") == 12
        for bad in (None, "", "  ", "-1", "1.5", "abc", "0x1", "1e3", " 1",
                    7, object()):
            assert parse_step_param(bad) is None

    def test_no_request_context_means_no_index(self):
        """The whole never-auto-fire property rests on this returning None."""
        from systemu.interface.tour import _active_page_step_index
        assert _active_page_step_index() is None


class TestNeverAutoFires:
    @pytest.fixture
    def calls(self, monkeypatch):
        from systemu.interface import tour
        seen = []
        monkeypatch.setattr(tour, "render_page_tour_card",
                            lambda *a, **k: seen.append((a, k)))
        monkeypatch.setattr(tour, "_current_persona_name", lambda: None)
        return seen

    def test_without_a_ptour_param_nothing_renders(self, calls):
        """No query param (the real state on every ordinary page load)."""
        from systemu.interface.tour import maybe_render_page_tour
        maybe_render_page_tour("/work")
        assert calls == []

    def test_out_of_range_indices_render_nothing(self, calls, monkeypatch):
        from systemu.interface import tour
        for idx in (5, 99, 1000):
            monkeypatch.setattr(tour, "_active_page_step_index",
                                lambda _i=idx: _i)
            tour.maybe_render_page_tour("/work")
        assert calls == []

    def test_a_route_without_hints_renders_nothing(self, calls, monkeypatch):
        from systemu.interface import tour
        monkeypatch.setattr(tour, "_active_page_step_index", lambda: 0)
        tour.maybe_render_page_tour("/settings")
        assert calls == []

    def test_an_in_range_index_renders_the_card_for_that_route(
            self, calls, monkeypatch):
        from systemu.interface import tour
        from systemu.interface.persona_content import DEFAULT_SKIN
        monkeypatch.setattr(tour, "_active_page_step_index", lambda: 0)
        tour.maybe_render_page_tour("/work")
        assert len(calls) == 1
        args, _kwargs = calls[0]
        assert args[0] == 0
        assert args[1] == "/work"
        assert args[2] == DEFAULT_SKIN.page_hints["/work"]

    def test_the_persona_decides_which_hints_render(self, calls, monkeypatch):
        from systemu.interface import tour
        from systemu.interface.persona_content import PERSONA_CONTENT
        monkeypatch.setattr(tour, "_active_page_step_index", lambda: 0)
        monkeypatch.setattr(tour, "_current_persona_name", lambda: "Personal")
        tour.maybe_render_page_tour("/tools")
        assert calls[0][0][2] == PERSONA_CONTENT["Personal"].page_hints["/tools"]

    def test_persona_resolution_failure_still_renders_the_default(
            self, calls, monkeypatch):
        from systemu.interface import tour
        from systemu.interface.persona_content import DEFAULT_SKIN

        def _boom():
            raise RuntimeError("no app state")

        monkeypatch.setattr(tour, "_active_page_step_index", lambda: 0)
        monkeypatch.setattr(tour, "_current_persona_name", _boom)
        tour.maybe_render_page_tour("/work")
        assert calls[0][0][2] == DEFAULT_SKIN.page_hints["/work"]


class TestPageToursAreStateless:
    """A page tour is a replayable throwaway: it must never record a fact."""

    def test_the_card_writes_no_completion_fact(self):
        from systemu.interface import tour
        src = inspect.getsource(tour.render_page_tour_card)
        assert "mark_tour_completed" not in src
        assert "add_fact" not in src

    def test_maybe_render_page_tour_writes_no_completion_fact(self):
        from systemu.interface import tour
        src = inspect.getsource(tour.maybe_render_page_tour)
        assert "mark_tour_completed" not in src

    def test_done_clears_to_the_bare_route(self):
        from systemu.interface import tour
        src = inspect.getsource(tour.render_page_tour_card)
        assert "ui.navigate.to(route)" in src, \
            "Done must drop the ?ptour param, leaving the operator on the page"

    def test_the_main_tour_completion_paths_are_untouched(self):
        from systemu.interface import tour
        src = inspect.getsource(tour.render_tour_card)
        assert "_complete(False)" in src and "_complete(True)" in src


# ---------------------------------------------------------------------------
# The "?" pill - pure model, rendered only where hints exist
# ---------------------------------------------------------------------------
class TestPillModel:
    def test_visible_with_a_target_where_hints_exist(self):
        from systemu.interface.tour import page_tour_pill_model
        m = page_tour_pill_model("/work", None)
        assert m["visible"] is True
        assert m["target"] == "/work?ptour=0"

    def test_invisible_where_the_persona_has_no_hints(self):
        from systemu.interface.tour import page_tour_pill_model
        for route in ("/settings", "/chat", "/inbox", "/nope"):
            m = page_tour_pill_model(route, None)
            assert m["visible"] is False
            assert m["target"] == ""

    def test_never_on_home(self):
        from systemu.interface.tour import page_tour_pill_model
        assert page_tour_pill_model("/", None)["visible"] is False

    def test_a_persona_only_hint_lights_the_pill_for_that_persona(self):
        from systemu.interface.tour import page_tour_pill_model
        # /insights is Enterprise-only copy; nobody else gets a pill there.
        assert page_tour_pill_model("/insights",
                                    "Enterprise professional")["visible"] is True
        assert page_tour_pill_model("/insights", "Personal")["visible"] is False

    def test_never_raises_on_hostile_input(self):
        from systemu.interface.tour import page_tour_pill_model
        for route in (None, 7, object()):
            assert page_tour_pill_model(route, object())["visible"] is False

    def test_query_strings_do_not_defeat_the_route_match(self):
        """The layout hands us the page path; a stale param must not matter."""
        from systemu.interface.tour import page_tour_pill_model
        m = page_tour_pill_model("/work?ptour=1", None)
        assert m["visible"] is True
        assert m["target"] == "/work?ptour=0"


# ---------------------------------------------------------------------------
# Reachability pins: remove the wiring and a NAMED test goes red
# ---------------------------------------------------------------------------
def _layout_called_names() -> set:
    """Every function name CALLED inside dashboard._build_layout (AST, not text).

    A mention in a comment or a docstring does not count - only a real call.
    """
    from systemu.interface import dashboard
    tree = ast.parse(textwrap.dedent(inspect.getsource(dashboard._build_layout)))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class TestLayoutWiring:
    def test_layout_calls_maybe_render_page_tour(self):
        assert "maybe_render_page_tour" in _layout_called_names(), \
            "?ptour=N must be consulted by the shared layout on every route"

    def test_layout_calls_the_page_tour_pill(self):
        assert "render_page_tour_pill" in _layout_called_names(), \
            "the '?' pill must render in the shared header, not per page"

    def test_the_main_tour_hooks_are_still_wired(self):
        called = _layout_called_names()
        assert "maybe_render_tour" in called
        assert "render_tour_pill" in called

    def test_the_pill_helper_consults_the_pure_model(self):
        from systemu.interface import tour
        src = inspect.getsource(tour.render_page_tour_pill)
        assert "page_tour_pill_model" in src, \
            "the renderer must not re-derive visibility next to the model"


class TestHonestyWall:
    def test_page_hint_copy_is_inside_the_registry_honesty_blob(self):
        """The ban list must actually SEE the new copy (DEC-34: a control that
        does not read the checked bytes is not a control)."""
        from test_persona_content_registry import all_skin_copy
        from systemu.interface.persona_content import DEFAULT_SKIN
        blob = all_skin_copy()
        for step in DEFAULT_SKIN.page_hints["/work"]:
            assert step["body"].lower() in blob
            assert step["title"].lower() in blob
