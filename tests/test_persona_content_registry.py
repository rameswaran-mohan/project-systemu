"""Persona registry: every personas() key has a complete skin; helper reads the fact."""
from systemu.interface.pages.welcome import personas, starter_prompts


def test_every_persona_has_a_complete_skin():
    from systemu.interface.persona_content import PERSONA_CONTENT, DEFAULT_SKIN
    for p in personas():
        skin = PERSONA_CONTENT[p]
        assert len(skin.starters) >= 3
        assert skin.dare_line
        assert skin.empty_work and skin.empty_shadows
        assert skin.tour_order and sorted(skin.tour_order) == list(range(6))
    assert DEFAULT_SKIN.starters == starter_prompts()


def test_skin_for_unknown_persona_is_default():
    from systemu.interface.persona_content import skin_for, DEFAULT_SKIN
    assert skin_for("") is DEFAULT_SKIN
    assert skin_for("Martian") is DEFAULT_SKIN


def test_current_persona_reads_newest_persona_fact(tmp_path):
    from systemu.interface.persona_content import current_persona

    class FakeVault:
        root = str(tmp_path)

    from systemu.runtime.user_profile import add_fact
    v = FakeVault()
    assert current_persona(v) is None
    add_fact(v, "Usage persona: Freelance", source="onboarding",
             tags=["office_context", "persona"])
    add_fact(v, "Usage persona: Personal", source="onboarding",
             tags=["office_context", "persona"])
    assert current_persona(v) == "Personal"          # newest wins


def test_no_skin_promises_unwired_capability():
    # Honesty wall: banned phrases that would advertise what is not built.
    from systemu.interface import persona_content as pc
    banned = ("share with your team", "multi-user", "coming soon", "will be able to")
    blob = " ".join(
        " ".join(s.starters) + s.dare_line + s.empty_work + s.empty_shadows
        for s in list(pc.PERSONA_CONTENT.values()) + [pc.DEFAULT_SKIN]
    ).lower()
    for phrase in banned:
        assert phrase not in blob
