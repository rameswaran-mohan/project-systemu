"""Task 5 - Settings persona switcher: the fact it writes, and the wiring.

The switch APPENDS a persona fact (source="settings"); newest-fact-wins in
`persona_content.current_persona` makes the re-skin live with no migration and
no rewrite of the onboarding answer.
"""
import inspect

from systemu.interface.persona_content import current_persona
from systemu.runtime.user_profile import add_fact, get_facts


class FakeVault:
    """Facts live under `vault.root` - that is the whole surface used here."""

    def __init__(self, root):
        self.root = str(root)


# ── The fact contract ────────────────────────────────────────────────────────
def test_persona_switch_appends_fact_and_newest_wins(tmp_path):
    v = FakeVault(tmp_path)
    add_fact(v, "Usage persona: Personal", source="onboarding", tags=["persona"])
    add_fact(v, "Usage persona: Enterprise professional", source="settings",
             tags=["persona"])
    assert current_persona(v) == "Enterprise professional"


def test_set_persona_writes_the_switch_fact_and_returns_the_notice(tmp_path):
    from systemu.interface.pages.settings import set_persona
    v = FakeVault(tmp_path)

    msg = set_persona(v, "Freelance")

    assert msg == "Content re-tuned for Freelance."
    assert msg.isascii()
    facts = get_facts(v, tags=["persona"])
    assert len(facts) == 1
    f = facts[-1]
    assert f.fact == "Usage persona: Freelance"
    assert f.source == "settings"
    assert set(f.tags) >= {"office_context", "persona"}
    assert current_persona(v) == "Freelance"


def test_switching_does_not_rewrite_the_onboarding_fact(tmp_path):
    from systemu.interface.pages.settings import set_persona
    v = FakeVault(tmp_path)
    add_fact(v, "Usage persona: Personal", source="onboarding",
             tags=["office_context", "persona"])

    set_persona(v, "Solo business")

    assert [f.fact for f in get_facts(v, tags=["persona"])] == [
        "Usage persona: Personal",
        "Usage persona: Solo business",
    ]
    assert current_persona(v) == "Solo business"


def test_blank_choice_is_a_no_op(tmp_path):
    from systemu.interface.pages.settings import set_persona
    v = FakeVault(tmp_path)
    assert set_persona(v, "") == ""
    assert set_persona(v, "   ") == ""
    assert set_persona(v, None) == ""
    assert get_facts(v, tags=["persona"]) == []
    assert current_persona(v) is None


def test_every_offered_persona_round_trips_to_a_real_skin(tmp_path):
    from systemu.interface.pages.settings import set_persona
    from systemu.interface.pages.welcome import personas
    from systemu.interface.persona_content import PERSONA_CONTENT, skin_for
    for i, p in enumerate(personas()):
        v = FakeVault(tmp_path / f"vault{i}")
        set_persona(v, p)
        assert current_persona(v) == p
        assert skin_for(current_persona(v)) is PERSONA_CONTENT[p]


# ── Reachability pins (GTM rule: delete the call site -> a named test reds) ───
def test_settings_source_offers_persona_switch():
    from systemu.interface.pages import settings
    assert "How you use Systemu" in inspect.getsource(settings)


def test_settings_page_actually_calls_the_switcher_wiring():
    """Delete any of these call sites and this test reds: a correct helper is
    worth nothing if the page never reaches it."""
    import ast
    from systemu.interface.pages import settings
    tree = ast.parse(inspect.getsource(settings.build_settings_page))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"set_persona", "personas", "current_persona"} <= called, sorted(called)
    assert "persona_content" in inspect.getsource(settings)   # registry, not a copy
