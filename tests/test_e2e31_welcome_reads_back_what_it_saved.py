"""e2e31 D-1: the wizard must read back what "Finish setup" wrote.

THE WITNESSED DEFECT. Completing /welcome (preset ``quality``, persona
``Solo business``, a name, an output folder) writes three things: the tier
vars into the operating home's ``.env``, a ``Usage persona:`` fact into
``user_facts.jsonl``, and ``user_profile.json``. Reloading /welcome -- with the
same daemon, and again after a restart -- showed Preset blank, "How will you
use Systemu?" blank, name/location/timezone/output-dir blank and the step-4
starters back to the default skin. The same page proved it had READ the tiers
one section above ("Heads up: your model tiers are set to run on ..."), so this
was never a storage failure: the widgets were simply built with no ``value=``.

THE PROPERTY pinned here:

    Every field the wizard collects is initialised from what the wizard saved,
    through the same registry/reader that wrote it -- and a Finish that changes
    nothing writes nothing.

The last clause is the sharp edge. The persona is an APPEND-only fact, so a
wizard that re-submits an unchanged answer grows a duplicate on every visit;
and a blank select, once the field prefills, means "the operator cleared it",
which must forget the fact rather than silently keep a stale one.

No UI is constructed here: the page's read-back builder and its persona writer
are consumed at their seams, against a real vault under ``tmp_path``.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.interface.pages import welcome
from systemu.vault.vault import Vault

_TIER_ENV = ("SYSTEMU_TIER1_MODEL", "SYSTEMU_TIER2_MODEL", "SYSTEMU_TIER3_MODEL")


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """A real vault, under tmp_path -- never the worktree's systemu/vault."""
    return Vault(str(tmp_path))


def _cfg(**kw):
    """The config attributes the read-back builder reads, and nothing else."""
    base = {"output_dir": "", "tier1_model": "", "tier2_model": "",
            "tier3_model": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _no_tier_vars(monkeypatch):
    for name in _TIER_ENV:
        monkeypatch.delenv(name, raising=False)


def _quality_tier_vars(monkeypatch) -> dict:
    from sharing_on.model_presets import PRESETS
    tiers = PRESETS["quality"]
    for i, name in enumerate(_TIER_ENV, start=1):
        monkeypatch.setenv(name, tiers[f"tier{i}"])
    return tiers


# ── the preset, derived from the vars the wizard wrote ───────────────────────

def test_saved_preset_names_the_preset_the_saved_tier_vars_spell(monkeypatch):
    """The dropdown's value comes from the SAME registry Finish wrote through,
    so it can never show a name whose tiers are not the ones on disk."""
    _quality_tier_vars(monkeypatch)
    assert welcome.saved_preset() == "quality"


def test_saved_preset_is_blank_when_the_wizard_never_wrote_the_vars(monkeypatch):
    """No saved state -> the pre-fix default (blank), not the shipped tiers.

    The budget preset IS the no-preset behaviour, so deriving the answer from
    the RESOLVED config would prefill "budget" on a machine whose operator has
    never opened step 2. The explicit vars are the only honest witness.
    """
    _no_tier_vars(monkeypatch)
    assert welcome.saved_preset() == ""


def test_saved_preset_is_blank_when_the_tiers_match_no_preset(monkeypatch):
    """An operator on hand-tuned tiers is told nothing rather than told wrong."""
    for name in _TIER_ENV:
        monkeypatch.setenv(name, "some/hand-tuned-model")
    assert welcome.saved_preset() == ""


def test_saved_preset_needs_all_three_vars(monkeypatch):
    """A partial write is not a preset -- two tiers of 'quality' are not it."""
    tiers = _quality_tier_vars(monkeypatch)
    monkeypatch.delenv("SYSTEMU_TIER3_MODEL", raising=False)
    assert tiers["tier3"]
    assert welcome.saved_preset() == ""


# ── every other field ────────────────────────────────────────────────────────

def test_initial_values_read_back_the_saved_profile_and_persona(
        vault, monkeypatch):
    _quality_tier_vars(monkeypatch)
    welcome.save_onboarding(
        vault, name="Ramesh", location="Chennai, IN",
        timezone="Asia/Kolkata", output_dir="D:/deliverables",
        persona="Solo business")

    values = welcome.welcome_initial_values(vault, _cfg())

    assert values["preset"] == "quality"
    assert values["persona"] == "Solo business"
    assert values["name"] == "Ramesh"
    assert values["location"] == "Chennai, IN"
    assert values["timezone"] == "Asia/Kolkata"
    assert values["output_dir"] == "D:/deliverables"


def test_saved_timezone_is_read_back_not_re_detected(vault, monkeypatch):
    """The wizard re-detected the timezone on every visit, which silently
    overwrote an operator who had corrected it."""
    _no_tier_vars(monkeypatch)
    monkeypatch.setattr(welcome, "detect_timezone", lambda: "Etc/GMT-14")
    welcome.save_onboarding(vault, name="R", location="", timezone="Europe/Oslo",
                            output_dir="")
    assert welcome.welcome_initial_values(vault, _cfg())["timezone"] == "Europe/Oslo"


def test_no_saved_state_keeps_todays_defaults(vault, monkeypatch):
    """A fresh install must render exactly the pre-fix page."""
    _no_tier_vars(monkeypatch)
    monkeypatch.setattr(welcome, "detect_timezone", lambda: "Asia/Kolkata")
    values = welcome.welcome_initial_values(vault, _cfg(output_dir="C:/out"))
    assert values == {"preset": "", "persona": "", "name": "", "location": "",
                      "timezone": "Asia/Kolkata", "output_dir": "C:/out"}


def test_initial_values_never_raise_on_an_unhappy_vault(monkeypatch):
    """The page shell must render even when the vault is broken."""
    _no_tier_vars(monkeypatch)
    monkeypatch.setattr(welcome, "detect_timezone", lambda: "UTC")
    values = welcome.welcome_initial_values(object(), object())
    assert values["name"] == "" and values["persona"] == ""
    assert values["timezone"] == "UTC"


def test_the_starters_follow_the_saved_persona(vault, monkeypatch):
    """Step 4's payoff: a returning operator sees THEIR kit, not the default."""
    from systemu.interface.persona_content import (DEFAULT_SKIN,
                                                   PERSONA_CONTENT, skin_for)
    _no_tier_vars(monkeypatch)
    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="Solo business")
    saved = welcome.welcome_initial_values(vault, _cfg())["persona"]
    skin = skin_for(saved)
    assert skin.starters == PERSONA_CONTENT["Solo business"].starters
    assert skin.starters != DEFAULT_SKIN.starters


# ── the persona fact: append-only, so a no-op Finish must write nothing ──────

def _persona_facts(vault):
    from systemu.runtime.user_profile import get_facts
    return get_facts(vault, tags=["persona"])


def test_an_unchanged_persona_writes_no_second_fact(vault, monkeypatch):
    _no_tier_vars(monkeypatch)
    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="Solo business")
    before = _persona_facts(vault)
    assert len(before) == 1

    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="Solo business")

    after = _persona_facts(vault)
    assert len(after) == 1, "re-submitting the prefilled answer duplicated it"
    assert after[0].id == before[0].id, "the original answer was rewritten"


def test_clearing_the_persona_forgets_the_fact(vault, monkeypatch):
    from systemu.interface.persona_content import current_persona
    _no_tier_vars(monkeypatch)
    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="Solo business")

    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="")

    assert _persona_facts(vault) == [], "a cleared select kept a stale fact"
    assert current_persona(vault) is None


def test_a_changed_persona_is_still_written(vault, monkeypatch):
    _no_tier_vars(monkeypatch)
    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="Solo business")
    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="Freelance")
    from systemu.interface.persona_content import current_persona
    assert current_persona(vault) == "Freelance"


def test_a_blank_persona_on_a_fresh_vault_writes_nothing(vault, monkeypatch):
    """Today's behaviour for the operator who skips step 3's first field."""
    _no_tier_vars(monkeypatch)
    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="")
    assert _persona_facts(vault) == []


def test_persona_write_plan_is_pure_and_total():
    assert welcome.persona_write_plan("", "Freelance") == welcome.PERSONA_WRITE
    assert welcome.persona_write_plan("Freelance", "Freelance") == welcome.PERSONA_KEEP
    assert welcome.persona_write_plan(" Freelance ", "Freelance") == welcome.PERSONA_KEEP
    assert welcome.persona_write_plan("Freelance", "") == welcome.PERSONA_FORGET
    assert welcome.persona_write_plan("", "") == welcome.PERSONA_KEEP
    assert welcome.persona_write_plan(None, None) == welcome.PERSONA_KEEP


def test_the_persona_writer_seam_is_not_called_for_a_no_op(vault, monkeypatch):
    """The witness the ruling asks for: no writer is reached at all."""
    _no_tier_vars(monkeypatch)
    welcome.save_onboarding(vault, name="R", location="", timezone="UTC",
                            output_dir="", persona="Freelance")

    calls = []
    welcome.apply_persona_choice(
        vault, "Freelance",
        add_fact_fn=lambda *a, **k: calls.append("add"),
        forget_fact_fn=lambda *a, **k: calls.append("forget"))
    assert calls == []

    welcome.apply_persona_choice(
        vault, "",
        add_fact_fn=lambda *a, **k: calls.append("add"),
        forget_fact_fn=lambda *a, **k: calls.append("forget"))
    assert calls == ["forget"]


# ── reachability: the widgets must actually consume the read-back ────────────

def test_every_wizard_field_is_built_from_the_read_back():
    """Delete a ``value=`` and this goes red (GTM standing rule)."""
    src = inspect.getsource(welcome.build_welcome_page)
    assert "welcome_initial_values(" in src, \
        "the page must read back what Finish saved"
    for widget, key in (("preset_select", "preset"), ("persona_in", "persona"),
                        ("name_in", "name"), ("loc_in", "location"),
                        ("tz_in", "timezone"), ("out_in", "output_dir")):
        block = src.split(f"{widget} = ")[1].split("\n\n")[0]
        assert f'_initial["{key}"]' in block, \
            f"{widget} must initialise from the saved {key}"


def test_the_page_no_longer_re_detects_the_timezone_inline():
    """``detect_timezone()`` is the FALLBACK inside the builder now, not the
    widget's value -- otherwise a corrected timezone is overwritten on reload."""
    src = inspect.getsource(welcome.build_welcome_page)
    assert "detect_timezone()" not in src, \
        "the timezone widget must take the read-back value, not a fresh detect"
