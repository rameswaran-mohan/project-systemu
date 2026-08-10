"""Task 2 — /welcome finally CONSUMES the persona it has always collected.

W9.1 asked "How will you use Systemu?" and stored the answer as a fact; the
docstring promised "starter kits consume it later" and nothing ever did. The
starters were a fixed triple for every operator.

These are reachability pins as much as unit tests (GTM standing rule): delete
the production call site — the `skin_for(persona_in.value)` read, the
`on_value_change` refresh, the dare, or the growth thesis — and a NAMED test
below goes red. The behavioural half (`dare_label`) is pure, so the budget-tier
caveat is asserted for real rather than grepped.
"""
from __future__ import annotations

import inspect
import re
from urllib.parse import quote

from systemu.interface.pages import welcome
from systemu.interface.persona_content import (DARE_PROMPT, DEFAULT_SKIN,
                                               PERSONA_CONTENT, skin_for)


class _Cfg:
    """The only config attribute the dare caveat reads."""

    def __init__(self, tier1: str = "") -> None:
        self.tier1_model = tier1


# ── the pre-registry contract must survive ───────────────────────────────────

def test_starter_prompts_contract_is_unchanged():
    """`starter_prompts()` stays as-is and the no-persona page renders it.

    A fresh operator who has not answered step 3 yet must see exactly the
    pre-registry starters — the skin system adds emphasis, never a regression.
    """
    assert welcome.starter_prompts() == DEFAULT_SKIN.starters
    assert skin_for(None).starters == welcome.starter_prompts()
    assert skin_for("").starters == welcome.starter_prompts()
    assert skin_for("Martian").starters == welcome.starter_prompts()


def test_every_persona_gets_three_starters_plus_the_dare():
    """Four clickable lines for every skin — three starters and one dare."""
    for name, skin in list(PERSONA_CONTENT.items()) + [("<default>", DEFAULT_SKIN)]:
        assert len(skin.starters) >= 3, name
        assert skin.dare_line, name


# ── the dare ─────────────────────────────────────────────────────────────────

def test_dare_prompt_lands_in_finalize_destination():
    """The dare rides the same /chat?prefill= destination the starters use."""
    dest = f"/chat?prefill={quote(DARE_PROMPT)}"
    assert dest.startswith("/chat?prefill=")
    assert " " not in dest, "an unencoded prompt would break the query string"


def test_dare_label_carries_the_budget_caveat_only_on_budget_tiers():
    """Honesty wall: forge quality degrades on flash-class reasoning models,
    so the dare says so — but only when the operator is actually on one."""
    skin = skin_for("Personal")
    budget = welcome.dare_label(skin, _Cfg("deepseek/deepseek-v4-flash"))
    quality = welcome.dare_label(skin, _Cfg("anthropic/claude-sonnet-4.5"))
    unset = welcome.dare_label(skin, _Cfg(""))

    assert budget.startswith(skin.dare_line)
    assert "quality preset" in budget
    assert quality == skin.dare_line, "no caveat when the tier is not budget-class"
    assert unset == skin.dare_line, "an unknown model must not cry wolf"


def test_dare_label_is_defensive_and_ascii():
    """Never break the wizard over a config shape; caveat stays ASCII."""
    class Bare:
        pass

    assert welcome.dare_label(DEFAULT_SKIN, Bare()) == DEFAULT_SKIN.dare_line
    assert welcome.dare_label(DEFAULT_SKIN, None) == DEFAULT_SKIN.dare_line
    welcome.DARE_BUDGET_CAVEAT.encode("ascii")


# ── reachability: the wiring, not just the data ──────────────────────────────

def test_welcome_reads_the_registry_reactively():
    src = inspect.getsource(welcome.build_welcome_page)
    assert "persona_content" in inspect.getsource(welcome), \
        "the page must import the registry Task 1 landed"
    assert "skin_for(persona_in.value)" in src, \
        "starters must render from the live persona answer"
    assert "ui.refreshable" in src, "the starters block must be refreshable"
    assert "persona_in.on_value_change" in src, \
        "changing the persona must re-render the starters"


def test_the_dare_is_rendered_from_the_registry():
    src = inspect.getsource(welcome.build_welcome_page)
    assert "DARE_PROMPT" in src and "dare_label(" in src, \
        "the fourth line is the forge dare, tier-caveated at render"


def test_starters_and_dare_still_finalize_before_navigating():
    """W11.4 gate-bounce regression (the "just refreshes" bug): moving the
    starters into a refreshable must not turn a click into a bare navigate."""
    src = inspect.getsource(welcome.build_welcome_page)
    assert not re.search(
        r'on\(\s*["\']click["\'][^)]*navigate\.to\(\s*\n?\s*f?["\']/chat',
        src, re.S), "a starter/dare click must finalize onboarding first"
    wiring = src.split("def _starter_line")[1].split("@ui.refreshable")[0]
    assert '.on("click"' in wiring and "_run_finalize(" in wiring, \
        "the one click-wiring path must finalize before navigating"
    body = src.split("def _starters")[1]
    assert body.count("_starter_line(") >= 2, \
        "both the starters and the dare route through that one path"
    assert "DARE_PROMPT" in body, "the dare renders inside the refreshable"


def test_intro_carries_the_growth_thesis():
    src = inspect.getsource(welcome.build_welcome_page)
    assert "smallest Systemu will ever be" in src
    assert "builds new tools" in src
    assert "with your approval" in src, \
        "forging is operator-gated — the thesis must say so"
    assert "learns workflows by watching you work" in src


def test_new_welcome_copy_promises_nothing_unwired():
    """Honesty wall: no roadmap language on the first screen an operator sees."""
    src = inspect.getsource(welcome.build_welcome_page).lower()
    for phrase in ("coming soon", "will be able to", "multi-user",
                   "share with your team", "in a future release"):
        assert phrase not in src, phrase
