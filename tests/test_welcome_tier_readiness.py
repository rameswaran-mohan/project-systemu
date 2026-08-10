"""Step 1 may not promise a machine the ROUTER cannot actually serve.

THE GAP LEFT BY DEC-43. The unification made satisfaction unconditional on
selection, which is right: a keyless provider that ANSWERS is usable on every
surface, whatever a tier currently names. On the machine the defect was found
on -- no keys, Ollama answering, the shipped default tier models -- /welcome
step 1 now says

    Provider ready: Ollama - you're set to run tasks.

and Finish proceeds. Both statements are true about SATISFACTION. But the
router routes by the TIER MODELS, and ``deepseek/deepseek-v4-flash`` is served
by OpenRouter, which has no key here. The operator finishes the wizard, clicks
the first starter, and the task fails.

Satisfaction and selection stay two facts (that separation is the DEC-43
ruling and this file does not touch it). What was missing is that the SECOND
fact had no surface on the page where a fresh operator stands: it is minted by
``provider_status.unusable_selected`` and rendered only in Settings.

THE PROPERTY pinned here:

    When step 1 says a provider is ready, it also says so when the tiers that
    provider does NOT serve would fail -- naming the provider the router will
    really call, and offering only remedies that exist.

The remedy clause is the sharp edge. "Pick a preset below that uses Ollama" is
a true sentence only while such a preset is in the step-2 dropdown, and today
none is: every shipped preset names cloud model ids. So the offer is COMPUTED
from ``PRESETS`` rather than written, and the tests below pin both branches --
including one that adds a local preset and watches the offer appear.

No network: the keyless witness is injected at the mint's own probe table, and
provider routing is a pure function of model ids and config attributes.
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

#: The shipped default on a fresh install -- served by OpenRouter, not Ollama.
DEFAULT_TIER_MODEL = "deepseek/deepseek-v4-flash"


@pytest.fixture(autouse=True)
def _bare_machine(monkeypatch):
    """Construct the machine these tests assert on; never inherit one.

    Same reasoning as tests/test_provider_verdict_unification.py -- the
    surfaces under test consult os.environ as well as the config, so the
    verdict would otherwise depend on what the rest of the suite left behind.
    Removes only.
    """
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


def _keyless_only(**kw):
    """No keys anywhere; a keyless endpoint configured."""
    return _machine(ollama_url="http://localhost:11434", **kw)


def _answering(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_REACHABLE, "answered at the test endpoint with 1 model(s)")


def _silent(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_UNREACHABLE, "nothing answered (test)")


def _statuses(cfg, probe):
    from systemu.runtime import provider_status as ps
    return ps.all_provider_statuses(cfg, probe=probe)


def _warning(cfg, probe):
    from systemu.interface.pages import welcome
    return welcome.tier_readiness_warning(_statuses(cfg, probe), cfg)


# -- the three machine shapes ------------------------------------------------

def test_a_shape_no_warning_when_the_tiers_run_on_what_is_satisfied():
    """A keyed machine whose tiers route to that same key. Nothing to say."""
    cfg = _machine(openrouter_api_key="sk-or-present")
    assert _warning(cfg, _silent) == ""


def test_a_shape_no_warning_for_the_operator_who_pointed_the_tiers_at_ollama():
    """The keyless operator who did the whole job must not be nagged.

    This is the shape DEC-43's remedy text tells people to build ("use Ollama
    (no key)"), and a warning here would be the mirror of the old defect: a
    refusal aimed at a machine that works.
    """
    cfg = _keyless_only(**{f"tier{i}_model": "ollama/llama3.1" for i in (1, 2, 3)})
    assert _warning(cfg, _answering) == ""


def test_b_shape_the_keyless_machine_with_cloud_tiers_is_warned_by_name():
    """THE gap. Ollama answers, so step 1 says "Provider ready: Ollama" -- and
    every tier is a model only OpenRouter serves, with no OpenRouter key."""
    warn = _warning(_keyless_only(), _answering)
    assert warn, "step 1 promised a machine whose tiers cannot run"
    assert "OpenRouter" in warn, (
        "the warning must name the provider the ROUTER will really call, not "
        f"a generic 'a provider': {warn!r}")
    assert "Ollama" in warn, warn
    warn.encode("ascii")   # DEC-32c: operator-visible copy is ASCII-only


def test_b_shape_names_the_native_provider_when_that_is_where_the_call_goes():
    """Routing is KEY-AWARE, and the warning inherits that.

    An OpenAI-only machine whose tier 1 is a ``google/*`` id really does send
    that call to Google (there is no OpenRouter key to fall back to), so
    Google is the honest name to print. A model-prefix guess written here
    would print the same thing on a machine where OpenRouter serves that id
    perfectly well -- the false-alarm half of this defect.
    """
    cfg = _machine(openai_api_key="sk-openai",
                   tier1_model="google/gemini-3-flash-preview",
                   tier2_model="gpt-4o-mini", tier3_model="gpt-4o-mini")
    warn = _warning(cfg, _silent)
    assert "Google" in warn, warn
    assert "OpenAI" in warn, ("the line has to say what IS working, or it "
                              f"reads as 'nothing works': {warn!r}")


def test_the_key_aware_fallback_is_honoured_so_no_false_alarm_is_raised():
    """The counterpart: the SAME tiers on an OpenRouter machine are fine.

    ``_resolve_provider_keyaware`` reroutes an unkeyed native id through
    OpenRouter, so warning here would be crying wolf about a working install.
    """
    cfg = _machine(openrouter_api_key="sk-or-present",
                   tier1_model="anthropic/claude-sonnet-4.5",
                   tier2_model="google/gemini-3-flash-preview",
                   tier3_model=DEFAULT_TIER_MODEL)
    assert _warning(cfg, _silent) == ""


def test_c_shape_a_machine_with_nothing_satisfied_gets_no_new_warning():
    """The no-provider banner owns that machine and is not doubled up on.

    Two banners saying different halves of "you cannot run" is the reader's
    version of two verdicts.
    """
    assert _warning(_keyless_only(), _silent) == ""
    assert _warning(_machine(), _silent) == ""


# -- the remedy clause: only offers that are real ----------------------------

def test_no_preset_is_offered_when_no_preset_can_serve_the_working_provider():
    """Today every shipped preset names a cloud model id, so "pick a preset
    below that uses Ollama" would be a lie about the dropdown right there on
    the same screen."""
    from sharing_on.model_presets import PRESETS
    warn = _warning(_keyless_only(), _answering)
    for name in PRESETS:
        assert f"'{name}'" not in warn, (
            f"offered the {name!r} preset as a remedy, but its tiers do not "
            f"run on this machine either: {warn!r}")
    assert "preset" in warn.lower(), (
        "the operator should still be told that switching preset is not the "
        f"way out here: {warn!r}")


def test_a_preset_that_does_run_here_is_offered_by_name(monkeypatch):
    """...and the offer is COMPUTED, not written: add a local preset and the
    same code starts naming it."""
    from sharing_on import model_presets
    monkeypatch.setitem(model_presets.PRESETS, "local",
                        {"tier1": "ollama/llama3.1", "tier2": "ollama/llama3.1",
                         "tier3": "ollama/llama3.1"})
    warn = _warning(_keyless_only(), _answering)
    assert "'local'" in warn, warn
    assert "step 2" in warn, ("the remedy must point at the control that is on "
                              f"this page: {warn!r}")


@pytest.mark.parametrize("extra", [
    {},
    {"local": {"tier1": "ollama/a", "tier2": "ollama/b", "tier3": "ollama/c"}},
    {"half": {"tier1": "ollama/a", "tier2": DEFAULT_TIER_MODEL,
              "tier3": "ollama/c"}},
])
def test_every_preset_the_warning_names_is_one_that_would_actually_work(
        extra, monkeypatch):
    """The general property behind both branches (DEC-34: an assertion of a
    remedy must be true). Whatever preset name the copy quotes, it is in the
    step-2 dropdown AND every one of its tiers routes to a satisfied provider
    -- the third case is a preset that is only PARTLY local, which must never
    be offered."""
    import re
    from sharing_on import model_presets
    from systemu.interface.pages import welcome
    from systemu.runtime import provider_status as ps

    presets = dict(model_presets.PRESETS)
    presets.update(extra)
    monkeypatch.setattr(model_presets, "PRESETS", presets)

    cfg = _keyless_only()
    statuses = _statuses(cfg, _answering)
    warn = welcome.tier_readiness_warning(statuses, cfg)
    usable = {s.provider for s in ps.satisfied_providers(statuses)}
    runnable = dict(welcome.presets_that_run_here(cfg, usable))
    assert "half" not in runnable, (
        "a preset whose tier 2 still needs a key does not run here")
    quoted_names = [q for q in re.findall(r"'([^']+)'", warn) if q in presets]
    for quoted in quoted_names:
        assert quoted in runnable, (
            f"offered {quoted!r}, whose tiers do not run here either: {warn!r}")
    if runnable:
        assert quoted_names, f"a real remedy existed and was not offered: {warn!r}"


def test_the_warning_never_tells_the_operator_to_click_a_button_that_is_absent():
    """Re-check renders only in the NO-provider branch. This line lives in the
    other branch, so naming it would send the operator hunting for a control
    that is not on their screen."""
    warn = _warning(_keyless_only(), _answering)
    assert "re-check" not in warn.lower(), warn


# -- it consumes the mint, it does not re-derive the fact --------------------

def test_the_line_is_minted_by_unusable_selected(monkeypatch):
    """DEC-43: the fact "the selected tiers cannot run" has ONE mint. Silence
    the mint and this surface goes silent with it -- which is what a consumer
    does and what a second copy would not."""
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(ps, "unusable_selected", lambda _st, _sel: [])
    assert _warning(_keyless_only(), _answering) == ""


def test_the_selection_it_feeds_the_mint_is_the_routers_own_resolution():
    """What this page scores is what the ROUTER will really call, not the
    explicit per-tier PROVIDER override -- which is empty on a fresh install.

    The derivation itself now lives in the mint beside the fact it feeds
    (``provider_status.routed_tier_providers``), because the Settings banner
    fed that same mint the override alone and the two surfaces disagreed
    about one machine; tests/test_provider_selection_parity.py pins the pair.
    """
    from systemu.runtime import provider_status as ps
    assert ps.routed_tier_providers(_keyless_only()) == \
        ["openrouter", "openrouter", "openrouter"]
    assert ps.routed_tier_providers(
        _keyless_only(**{f"tier{i}_model": "ollama/llama3.1"
                         for i in (1, 2, 3)})) == ["ollama", "ollama", "ollama"]


def test_an_explicit_provider_override_still_wins():
    """``SYSTEMU_TIER{N}_PROVIDER`` is an override the router obeys literally,
    so the warning must obey it too."""
    from systemu.runtime import provider_status as ps
    cfg = _machine(openrouter_api_key="k", tier2_provider="ollama")
    assert ps.routed_tier_providers(cfg)[1] == "ollama"
    warn = _warning(cfg, _silent)
    assert "Ollama" in warn, ("tier 2 is pinned to a dead Ollama and would "
                              f"fail: {warn!r}")


def test_nothing_raises_into_the_page_when_resolution_blows_up(monkeypatch):
    """CARE: a warning is not worth a broken first screen."""
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(ps, "routed_tier_providers",
                        lambda _cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _warning(_keyless_only(), _answering) == ""
    assert ps.routed_provider("x/y", "", object()) in ("openrouter", "")


# -- RENDER PINS: remove the production call site and these go red -----------

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
    """Records every ``ui.<fn>(...)`` the wizard makes."""

    def __init__(self):
        self.calls, self.classes = [], []
        self.navigate = SimpleNamespace(to=lambda *a, **k: None)

    def refreshable(self, fn):
        return _Refreshable(fn)

    def timer(self, *a, **k):
        return _Node(self, "timer")

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self, name)
        return _call

    def labels(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "label" and a]


def _render_welcome(cfg, probe, monkeypatch) -> _RecordingUI:
    """Execute the REAL wizard against a recording ``ui``.

    Every real collaborator is imported BEFORE the fake nicegui goes into
    sys.modules, so none of them binds the recorder as its own ``ui``.
    """
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


def test_render_the_wizard_prints_the_warning_under_the_ready_line(monkeypatch):
    """THE reachability pin for the whole file: delete the ui.label and this
    goes red, however green the unit tests above stay."""
    rec = _render_welcome(_keyless_only(), _answering, monkeypatch)
    texts = rec.labels()
    ready = [t for t in texts if t.startswith("Provider ready:")]
    warn = [t for t in texts if t.startswith("Heads up:")]
    assert ready and "Ollama" in ready[0], texts
    assert warn, "the wizard rendered 'you're set to run tasks' and nothing else"
    assert "OpenRouter" in warn[0], warn
    assert texts.index(ready[0]) < texts.index(warn[0]), \
        "the caveat belongs under the claim it qualifies"


def test_render_a_healthy_install_shows_the_ready_line_alone(monkeypatch):
    rec = _render_welcome(_machine(openrouter_api_key="sk-or-present"),
                          _silent, monkeypatch)
    texts = rec.labels()
    assert [t for t in texts if t.startswith("Provider ready:")], texts
    assert not [t for t in texts if t.startswith("Heads up:")], texts


def test_render_the_no_provider_banner_is_unchanged_and_not_doubled(monkeypatch):
    """Shape (c) at the render: the existing banner, and only it."""
    rec = _render_welcome(_keyless_only(), _silent, monkeypatch)
    texts = rec.labels()
    banner = [t for t in texts if "No LLM provider is usable yet" in t]
    assert len(banner) == 1, texts
    assert "OPENROUTER_API_KEY" in banner[0], banner
    assert not [t for t in texts if t.startswith("Heads up:")], texts
    assert not [t for t in texts if t.startswith("Provider ready:")], texts
