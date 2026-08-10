"""Home "Your Systemu grows" card - capability odometer + getting-started quests.

The card has two halves and both are driven by PURE helpers that live in
``systemu/interface/pages/console.py`` so these tests need no NiceGUI runtime
(the same convention as ``tests/test_home_page.py``):

  * ``growth_counts(vault)``  -> {"tools": N, "shadows": N, "skills": N}
  * ``quest_states(vault)``   -> [(label, done), (label, done), (label, done)]

Both are defensive: a vault that raises (or is missing the method entirely)
yields zeros / not-done rather than breaking the Home shell.

The last two tests are WIRING pins, not unit tests: delete the production call
site and they go red.  A helper nothing renders is exactly the half-built shape
this card is meant to avoid.
"""
from __future__ import annotations

import inspect

import pytest

from systemu.interface.pages import console
from systemu.interface.pages.console import growth_counts, quest_states


# ---------------------------------------------------------------------------
#  Fixtures / stand-ins
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_ambient_workflows(monkeypatch):
    """The third quest reads work.py's workflow list.  ``WorkflowTracker`` is a
    process singleton, so without this an unrelated earlier test could seed it
    and make these assertions depend on suite ordering."""
    from systemu.interface.pages import work
    monkeypatch.setattr(work, "_load_rows", lambda: [], raising=True)


class _Vault:
    """Minimal vault stand-in - only the five reads the card makes."""

    def __init__(self, *, tools=(), shadows=(), skills=(), profile=None):
        self._tools = list(tools)
        self._shadows = list(shadows)
        self._skills = list(skills)
        self._profile = profile

    def list_tools(self, status=None):
        return list(self._tools)

    def list_shadows(self, status=None):
        return list(self._shadows)

    def list_skills(self):
        return list(self._skills)

    def get_user_profile(self):
        return self._profile


class _Exploding:
    """Every read raises - the card must still render zeros / not-done."""

    def list_tools(self, status=None):
        raise RuntimeError("vault down")

    def list_shadows(self, status=None):
        raise RuntimeError("vault down")

    def list_skills(self):
        raise RuntimeError("vault down")

    def get_user_profile(self):
        raise RuntimeError("vault down")


# ---------------------------------------------------------------------------
#  The odometer
# ---------------------------------------------------------------------------


def test_growth_counts_and_quests_are_pure_and_defensive(tmp_path):
    """The plan's Step-1 test, verbatim."""
    from systemu.interface.pages.console import growth_counts, quest_states

    class EmptyVault:
        root = str(tmp_path)

        def list_tools(self, status=None):
            return []

        def list_shadows(self, status=None):
            return []

        def list_skills(self):
            return []

        def get_user_profile(self):
            return None

    c = growth_counts(EmptyVault())
    assert c == {"tools": 0, "shadows": 0, "skills": 0}
    qs = quest_states(EmptyVault())
    assert len(qs) == 3 and all(done is False for _, done in qs)


class TestGrowthCounts:
    def test_counts_are_the_row_counts(self):
        v = _Vault(tools=[{}, {}, {}], shadows=[{}], skills=[{}, {}])
        assert growth_counts(v) == {"tools": 3, "shadows": 1, "skills": 2}

    def test_none_from_a_backend_reads_as_zero_not_a_crash(self):
        class _NoneVault(_Vault):
            def list_tools(self, status=None):
                return None

            def list_shadows(self, status=None):
                return None

            def list_skills(self):
                return None

        assert growth_counts(_NoneVault()) == {"tools": 0, "shadows": 0, "skills": 0}

    def test_a_raising_vault_yields_zeros(self):
        assert growth_counts(_Exploding()) == {"tools": 0, "shadows": 0, "skills": 0}

    def test_a_vault_missing_the_methods_yields_zeros(self):
        assert growth_counts(object()) == {"tools": 0, "shadows": 0, "skills": 0}

    def test_keys_are_exactly_the_three_counters(self):
        assert set(growth_counts(_Vault())) == {"tools", "shadows", "skills"}


class TestForgedToolCount:
    """`list_tools()` rows carry `forged_by_systemu` on every backend (it is
    emitted by `_tool_header`), so the odometer may honestly say how many of
    the tools Systemu built."""

    def test_counts_only_rows_flagged_forged_by_systemu(self):
        v = _Vault(tools=[
            {"id": "a", "forged_by_systemu": True},
            {"id": "b", "forged_by_systemu": False},
            {"id": "c"},
            {"id": "d", "forged_by_systemu": True},
        ])
        assert console.forged_tool_count(v) == 2

    def test_no_forged_tools_is_zero(self):
        assert console.forged_tool_count(_Vault(tools=[{"id": "a"}])) == 0

    def test_a_raising_vault_yields_zero(self):
        assert console.forged_tool_count(_Exploding()) == 0


class TestOdometerLine:
    """This source stays ASCII-only, so the separator is pinned by codepoint
    rather than by pasting the glyph into an assertion."""

    def test_separator_is_the_house_dot(self):
        dot = console._ODOMETER_DOT.strip()
        assert len(dot) == 1
        # U+2022 BULLET (already rendered elsewhere in console.py) or U+00B7
        assert ord(dot) in (0x2022, 0x00B7)

    def test_line_names_all_three_counters_with_their_numbers(self):
        dot = console._ODOMETER_DOT
        line = console._odometer_line({"tools": 3, "shadows": 1, "skills": 2}, 0)
        assert line == f"Tools 3{dot}Shadows 1{dot}Skills 2"

    def test_forged_clause_appended_only_when_some_tool_was_forged(self):
        counts = {"tools": 3, "shadows": 1, "skills": 2}
        assert "built for you" not in console._odometer_line(counts, 0)
        assert console._odometer_line(counts, 2).endswith("- 2 built for you")

    def test_forged_clause_is_singular_safe(self):
        line = console._odometer_line({"tools": 1, "shadows": 0, "skills": 0}, 1)
        assert line.endswith("- 1 built for you")


# ---------------------------------------------------------------------------
#  The quests
# ---------------------------------------------------------------------------


class TestQuestStates:
    def test_exactly_three_quests_in_plan_order(self):
        labels = [label for label, _ in quest_states(_Vault())]
        assert labels == [
            "Set up your profile",
            "Meet the six rooms (tour)",
            "Hand over your first task",
        ]

    def test_profile_quest_is_done_once_a_profile_exists(self):
        v = _Vault(profile={"name": "Ada"})
        assert dict(quest_states(v))["Set up your profile"] is True

    def test_profile_quest_is_not_done_without_one(self):
        assert dict(quest_states(_Vault()))["Set up your profile"] is False

    def test_tour_quest_reads_first_run_tour_completed(self, monkeypatch):
        from systemu.runtime import first_run
        monkeypatch.setattr(first_run, "tour_completed", lambda vault: True)
        assert dict(quest_states(_Vault()))["Meet the six rooms (tour)"] is True
        monkeypatch.setattr(first_run, "tour_completed", lambda vault: False)
        assert dict(quest_states(_Vault()))["Meet the six rooms (tour)"] is False

    def test_first_task_quest_reads_the_same_source_work_py_renders(self, monkeypatch):
        """Reachability pin: the quest must consult work.py's own row loader, not
        a second private copy of "has the operator handed over a task"."""
        from systemu.interface.pages import work
        monkeypatch.setattr(work, "_load_rows", lambda: [{"workflow_id": "wf_1"}])
        assert dict(quest_states(_Vault()))["Hand over your first task"] is True
        monkeypatch.setattr(work, "_load_rows", lambda: [])
        assert dict(quest_states(_Vault()))["Hand over your first task"] is False

    def test_done_flags_are_real_booleans(self):
        v = _Vault(profile={"name": "Ada"})
        assert all(isinstance(done, bool) for _, done in quest_states(v))

    def test_a_raising_vault_leaves_every_quest_not_done(self):
        qs = quest_states(_Exploding())
        assert len(qs) == 3 and all(done is False for _, done in qs)

    def test_a_raising_tour_check_does_not_break_the_card(self, monkeypatch):
        from systemu.runtime import first_run

        def _boom(vault):
            raise RuntimeError("no profile store")

        monkeypatch.setattr(first_run, "tour_completed", _boom)
        assert dict(quest_states(_Vault()))["Meet the six rooms (tour)"] is False


# ---------------------------------------------------------------------------
#  Wiring pins - the helpers must actually reach the operator's screen
# ---------------------------------------------------------------------------


def test_growth_card_is_rendered_by_the_home_page():
    src = inspect.getsource(console.build_home_page)
    assert "_build_growth_card(" in src, \
        "the growth card exists but Home never renders it"


def test_growth_card_composes_the_pure_helpers_and_retires_finished_quests():
    src = inspect.getsource(console._build_growth_card)
    assert "Your Systemu grows" in src
    assert "growth_counts(" in src and "quest_states(" in src
    assert "_odometer_line(" in src
    # self-retiring: nothing renders once all three quests are done
    assert "all(" in src
