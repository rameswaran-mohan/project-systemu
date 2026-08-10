"""Task 4 - the Work and Shadows empty states read their copy from the registry.

Two things are pinned here:

1. REACHABILITY (GTM standing rule): the production call sites must exist.
   Delete the registry lookup from either page and a named test below goes
   red - a green suite can never mean "the wiring is there" by itself.
2. BYTE-IDENTITY of the no-persona path.  DEFAULT_SKIN reproduces the exact
   literals both pages used before the registry, so an operator who never
   answered the persona question sees precisely what they saw yesterday.
   The two module constants below quote that copy VERBATIM, em-dash and
   all - the one place non-ASCII is intended in this file.
"""

import inspect

_OLD_WORK = "No workflows yet —"
_OLD_SHADOWS = (
    "No shadows yet — process a scroll to create your first shadow."
)


class _FakeVault:
    """Minimal vault stand-in - user_profile only needs ``root``."""

    def __init__(self, root):
        self.root = str(root)


class _BrokenVault:
    @property
    def root(self):
        raise RuntimeError("boom")


def test_empty_states_come_from_registry():
    from systemu.interface.pages import army, work

    work_src = inspect.getsource(work)
    army_src = inspect.getsource(army)

    assert "empty_work" in work_src
    assert "persona_content" in work_src
    assert "empty_shadows" in army_src
    assert "persona_content" in army_src

    # The hard-coded copy is no longer what either page RENDERS.  (work.py
    # still names "No workflows yet" in a comment: it is the anchor for the
    # W11 empty-state pin in tests/test_wave11_intuitive.py, which splits the
    # module source on that phrase to prove the "/chat" link sits beside the
    # copy.  Pin the render call instead of the phrase's absence.)
    assert "process a scroll" not in army_src          # jargon retired
    assert 'ui.label("No workflows yet' not in work_src
    assert 'ui.label("No shadows yet' not in army_src


def test_the_render_call_sites_are_the_ones_that_bite():
    """Pin the PRODUCTION call site, not just the helper's existence.

    Deleting ``_empty_work_text()`` from ``build_work_page`` (or the Shadows
    equivalent) leaves the helper importable and every other test green -
    this one goes red.
    """
    from systemu.interface.pages import army, work

    assert "_empty_work_text()" in inspect.getsource(work.build_work_page)
    assert "_empty_shadows_text(vault)" in inspect.getsource(army.build_army_page)


def test_default_skin_reproduces_the_retired_literals():
    from systemu.interface.persona_content import DEFAULT_SKIN

    assert DEFAULT_SKIN.empty_work == _OLD_WORK
    assert DEFAULT_SKIN.empty_shadows == _OLD_SHADOWS


def test_work_empty_state_follows_the_recorded_persona(tmp_path):
    from systemu.interface.pages.work import _empty_work_text
    from systemu.interface.persona_content import DEFAULT_SKIN, PERSONA_CONTENT
    from systemu.runtime.user_profile import add_fact

    vault = _FakeVault(tmp_path)
    assert _empty_work_text(vault) == DEFAULT_SKIN.empty_work

    skinned = PERSONA_CONTENT["Solo business"].empty_work
    assert skinned != DEFAULT_SKIN.empty_work      # the assertion below can bite

    add_fact(vault, "Usage persona: Solo business", source="onboarding",
             tags=["office_context", "persona"])
    assert _empty_work_text(vault) == skinned


def test_shadows_empty_state_follows_the_recorded_persona(tmp_path):
    from systemu.interface.pages.army import _empty_shadows_text
    from systemu.interface.persona_content import DEFAULT_SKIN, PERSONA_CONTENT
    from systemu.runtime.user_profile import add_fact

    vault = _FakeVault(tmp_path)
    assert _empty_shadows_text(vault) == DEFAULT_SKIN.empty_shadows

    skinned = PERSONA_CONTENT["Enterprise professional"].empty_shadows
    assert skinned != DEFAULT_SKIN.empty_shadows   # the assertion below can bite

    add_fact(vault, "Usage persona: Enterprise professional", source="onboarding",
             tags=["office_context", "persona"])
    assert _empty_shadows_text(vault) == skinned


def test_empty_state_helpers_fall_back_instead_of_raising():
    from systemu.interface.pages.army import _empty_shadows_text
    from systemu.interface.pages.work import _empty_work_text
    from systemu.interface.persona_content import DEFAULT_SKIN

    assert _empty_work_text(_BrokenVault()) == DEFAULT_SKIN.empty_work
    assert _empty_shadows_text(_BrokenVault()) == DEFAULT_SKIN.empty_shadows
