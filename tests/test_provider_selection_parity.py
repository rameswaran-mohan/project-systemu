"""One SELECTION, two surfaces: /welcome and Settings must ask the same question.

THE LAST DEC-43 SPLIT. ``provider_status.unusable_selected`` is the mint for
the second of the two facts the module keeps apart -- not "is a provider
usable" but "can the tiers this machine is SET TO actually run". It takes the
selection as an argument, and its two consumers were handing it different
answers to that word:

  * /welcome fed it the ROUTED per-tier providers -- what ``llm_router`` will
    really call, key-aware -- and warned;
  * Settings fed it the explicit ``tier{N}_provider`` OVERRIDES alone, which
    are EMPTY on a fresh install. So the red flag stayed dark on exactly the
    machine the defect was reported from: no keys, Ollama answering, the
    shipped default (OpenRouter-served) tier models. The operator who went
    looking in Settings for why their first task failed was told nothing.

That is DEC-43 form (i) one layer out: one fact, one mint, but two DERIVATIONS
of the mint's input, so the two surfaces could not agree. The derivation now
lives in the mint too (``provider_status.routed_tier_providers``) and both
surfaces consume it.

THE PROPERTY pinned here:

    For one config, the selection /welcome hands ``unusable_selected`` and the
    selection Settings hands it are the SAME list -- and each surface's banner
    follows that one derivation wherever it moves.

Both surfaces are exercised through their REAL render paths against a
recording nicegui, so deleting a production call site turns a named test red.
No network: the keyless witness is injected at the mint's own probe table and
provider routing is a pure function of model ids and config attributes.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from types import ModuleType, SimpleNamespace

import pytest

#: The shipped default on a fresh install -- served by OpenRouter, not Ollama.
DEFAULT_TIER_MODEL = "deepseek/deepseek-v4-flash"

#: What each surface calls the line it renders from ``unusable_selected``.
_BANNER_PREFIX = {"welcome": "Heads up:",
                  "settings": "Selected for a tier but not usable"}


@pytest.fixture(autouse=True)
def _bare_machine(monkeypatch):
    """Construct the machine these tests assert on; never inherit one."""
    from systemu.runtime import provider_status as ps
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    for i in (1, 2, 3):
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_PROVIDER", raising=False)
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_MODEL", raising=False)
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


def _machine(**kw):
    """A config view carrying exactly what the mint and the router read."""
    from systemu.runtime import provider_status as ps
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    base.update({f"tier{i}_model": DEFAULT_TIER_MODEL for i in (1, 2, 3)})
    base.update(output_dir="", non_interactive=False, vault_dir="/tmp/v")
    base.update(kw)
    return SimpleNamespace(**base)


def _the_reported_machine(**kw):
    """THE machine shape from the field report: no key anywhere, a keyless
    endpoint that ANSWERS, and the shipped cloud tier models. Nothing is
    overridden per tier -- which is what made Settings silent."""
    return _machine(ollama_url="http://localhost:11434", **kw)


def _answering(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_REACHABLE, "answered at the test endpoint with 1 model(s)")


# -- a recording nicegui, shared by both render harnesses --------------------

class _Node:
    def __init__(self, rec, kind):
        self._rec, self._kind = rec, kind
        self.value = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def classes(self, *a, **k):
        if a:
            self._rec.classes.append(str(a[0]))
        return self

    def style(self, *a, **k):
        return self

    def props(self, *a, **k):
        return self

    def tooltip(self, *a, **k):
        return self

    def on(self, *a, **k):
        return self

    def on_value_change(self, *a, **k):
        return self


class _Refreshable:
    def __init__(self, fn):
        self._fn = fn

    def __call__(self, *a, **k):
        return self._fn(*a, **k)

    def refresh(self, *a, **k):
        return self._fn(*a, **k)


class _RecordingUI:
    """Records every ``ui.<fn>(...)`` a page makes, and every timer it arms."""

    def __init__(self):
        self.calls, self.classes, self.timers = [], [], []
        self.navigate = SimpleNamespace(to=lambda *a, **k: None)

    def refreshable(self, fn):
        return _Refreshable(fn)

    def timer(self, _delay, callback=None, **k):
        if callback is not None:
            self.timers.append(callback)
        return _Node(self, "timer")

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self, name)
        return _call

    def labels(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "label" and a]


def _render_welcome(cfg, probe, monkeypatch) -> _RecordingUI:
    """Execute the REAL wizard against a recording ``ui``."""
    from systemu.interface import persona_content            # noqa: F401
    from systemu.interface.dashboard_state import AppState
    from systemu.interface.design import primitives
    from systemu.interface.pages import welcome
    from systemu.runtime import provider_status as ps

    monkeypatch.setitem(ps._PROBES, "ollama", probe)
    ps.clear_probe_cache()
    monkeypatch.setattr(AppState, "_instance",
                        SimpleNamespace(vault=SimpleNamespace(), config=cfg),
                        raising=False)
    rec = _RecordingUI()
    monkeypatch.setattr(primitives, "button",
                        lambda *a, **k: _Node(rec, "button"))
    fake = ModuleType("nicegui")
    fake.ui = rec
    monkeypatch.setitem(sys.modules, "nicegui", fake)
    welcome.build_welcome_page()
    return rec


def _render_settings(cfg, probe, monkeypatch) -> _RecordingUI:
    """Execute the REAL credentials card, INCLUDING its second paint.

    The banner is deliberately held back until the probes have run, so the
    timer the card arms has to be fired or this surface never reaches the mint
    at all -- and a test that never reached it would pass no matter what.
    """
    from systemu.interface.pages import settings
    from systemu.runtime import provider_status as ps

    monkeypatch.setitem(ps._PROBES, "ollama", probe)
    ps.clear_probe_cache()
    rec = _RecordingUI()
    monkeypatch.setattr(settings, "ui", rec)
    settings.provider_credentials_card(cfg)
    assert rec.timers, "the card no longer arms its observe pass"
    for callback in list(rec.timers):
        asyncio.run(callback())
    return rec


def _banners(surface, cfg, probe, monkeypatch) -> list:
    """The lines THIS surface renders out of ``unusable_selected``."""
    rec = (_render_welcome(cfg, probe, monkeypatch) if surface == "welcome"
           else _render_settings(cfg, probe, monkeypatch))
    return [t for t in rec.labels() if t.startswith(_BANNER_PREFIX[surface])]


def _spy_on_the_mint(monkeypatch) -> list:
    """Record the SELECTION each surface hands ``unusable_selected``."""
    from systemu.runtime import provider_status as ps
    real = ps.unusable_selected
    seen: list = []

    def _spy(statuses, selected):
        seen.append([str(x) for x in (selected or ())])
        return real(statuses, selected)

    monkeypatch.setattr(ps, "unusable_selected", _spy)
    return seen


# -- THE SPLIT -------------------------------------------------------------

def test_both_surfaces_hand_the_mint_the_same_selection(monkeypatch):
    """THE property. One config, one derivation of "what is selected".

    Before this fix the wizard passed ``['openrouter'] * 3`` and Settings
    passed ``['', '', '']`` -- the same mint, asked two different questions,
    which is how one screen warned and the other stayed silent about the same
    machine at the same moment.
    """
    seen = _spy_on_the_mint(monkeypatch)
    cfg = _the_reported_machine()

    _render_welcome(cfg, _answering, monkeypatch)
    from_welcome = list(seen)
    seen.clear()
    _render_settings(cfg, _answering, monkeypatch)
    from_settings = list(seen)

    assert from_welcome, "the wizard never consulted the selection mint"
    assert from_settings, "Settings never consulted the selection mint"
    assert from_welcome[-1] == from_settings[-1], (
        f"one config, two selections: welcome fed {from_welcome[-1]!r} and "
        f"settings fed {from_settings[-1]!r}")
    assert from_welcome[-1] == ["openrouter"] * 3, (
        "the selection both surfaces score must be what the ROUTER will call "
        f"for the shipped default tiers: {from_welcome[-1]!r}")


def test_settings_flags_the_fresh_install_it_used_to_stay_silent_on(monkeypatch):
    """The consequence, at the Settings render. No tier override is set, so the
    old feed was empty and this banner never appeared -- on the one machine
    whose tiers genuinely cannot run."""
    lines = _banners("settings", _the_reported_machine(), _answering, monkeypatch)
    assert lines, ("Settings rendered no selection banner on a machine whose "
                   "every tier routes to an unkeyed provider")
    assert "OpenRouter" in lines[0], lines
    assert "OPENROUTER_API_KEY" in lines[0], (
        "the banner carries the mint's own remedy detail, so it must name the "
        f"env var rather than a recipe written here: {lines[0]!r}")


@pytest.mark.parametrize("surface", ["welcome", "settings"])
def test_the_explicit_override_is_still_honoured_on_both(surface, monkeypatch):
    """The old Settings input is not thrown away, it is SUBSUMED: the router
    obeys ``SYSTEMU_TIER{N}_PROVIDER`` literally, so a tier pinned to a dead
    provider is still flagged -- now on both surfaces rather than one."""
    cfg = _the_reported_machine(openrouter_api_key="sk-or-present",
                                tier2_provider="google")
    lines = _banners(surface, cfg, _answering, monkeypatch)
    assert lines, f"{surface} ignored a tier pinned to an unkeyed provider"
    assert "Google" in lines[0], lines


# -- consumption pins: neither surface may re-derive the selection ----------

@pytest.mark.parametrize("surface", ["welcome", "settings"])
def test_each_surface_goes_silent_when_the_derivation_says_nothing(
        surface, monkeypatch):
    """Silence ``routed_tier_providers`` and BOTH banners go with it. A surface
    still speaking here would be one holding a second copy of the derivation.
    """
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(ps, "routed_tier_providers", lambda _cfg: [])
    assert _banners(surface, _the_reported_machine(), _answering,
                    monkeypatch) == []


@pytest.mark.parametrize("surface", ["welcome", "settings"])
def test_each_surface_follows_the_derivation_when_it_moves(surface, monkeypatch):
    """...and they move together: point the derivation somewhere else and both
    banners name the provider it now points at."""
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(ps, "routed_tier_providers",
                        lambda _cfg: ["anthropic", "anthropic", "anthropic"])
    lines = _banners(surface, _the_reported_machine(), _answering, monkeypatch)
    assert lines, f"{surface} stopped consuming the derivation"
    assert "Anthropic" in lines[0], lines
    assert "OpenRouter" not in lines[0], (
        f"{surface} named a provider the derivation did not: {lines[0]!r}")


def test_the_derivation_lives_in_the_mint_and_not_on_a_page():
    """DEC-43 form (i): the tree may not grow a second home for it. Both pages
    are checked, since either one re-adopting it recreates the split."""
    from systemu.interface.pages import settings, welcome
    from systemu.runtime import provider_status as ps

    assert callable(getattr(ps, "routed_tier_providers", None))
    assert callable(getattr(ps, "routed_provider", None))
    for page in (welcome, settings):
        src = inspect.getsource(page)
        assert "def routed_tier_providers" not in src, page.__name__
        assert "def routed_provider" not in src, page.__name__


def test_the_router_is_reached_through_a_public_seam():
    """The mint asks the router, and asks it by a name the router publishes --
    a runtime module reaching into another module's private name is a coupling
    nobody is obliged to keep working. The old private name stays as an alias
    so no in-tree caller is broken by the promotion."""
    from systemu.core import llm_router

    public = getattr(llm_router, "resolve_provider_keyaware", None)
    assert callable(public), "llm_router publishes no key-aware resolution"
    assert llm_router._resolve_provider_keyaware is public, (
        "the private name must remain an alias of the promoted one")


def test_the_derivation_is_key_aware_the_way_the_router_is():
    """It inherits the router's fallback rather than guessing from the model
    prefix, so it neither misses (an unkeyed native id) nor cries wolf (the
    same id on a box OpenRouter serves it from)."""
    from systemu.runtime import provider_status as ps

    assert ps.routed_tier_providers(_the_reported_machine()) == \
        ["openrouter"] * 3
    assert ps.routed_tier_providers(_machine(
        openrouter_api_key="sk-or-present",
        tier1_model="anthropic/claude-sonnet-4.5",
        tier2_model="google/gemini-3-flash-preview",
        tier3_model=DEFAULT_TIER_MODEL)) == ["openrouter"] * 3
    assert ps.routed_tier_providers(_machine(
        openai_api_key="sk-openai",
        tier1_model="google/gemini-3-flash-preview",
        tier2_model="gpt-4o-mini",
        tier3_model="gpt-4o-mini")) == ["google", "openai", "openai"]


def test_the_derivation_never_raises_into_a_render():
    """A banner is not worth a broken page. Handed nonsense it still returns
    three ids the mint's own table knows -- or "", which is ignored downstream
    -- and never raises into either render."""
    from systemu.runtime import provider_status as ps

    known = {s.provider for s in ps.PROVIDER_SPECS} | {""}
    assert ps.routed_provider(None, None, object()) in known
    tiers = ps.routed_tier_providers(object())
    assert len(tiers) == 3 and set(tiers) <= known, tiers
