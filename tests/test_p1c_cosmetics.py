"""Phase 1c cosmetics - two operator-visible surfaces, pinned.

  * THE FINALIZE REFUSAL IS PURE ASCII.  `/welcome`'s Finish gate is the first
    sentence a blocked operator reads, and it is also the sentence most likely
    to leave the browser: it is logged, and the same wording is echoed by the
    console setup flow onto a Windows terminal whose default code page cannot
    encode an em dash.  The pin is on the WHOLE assembled message (the fixed
    lead plus the provider hint), not on a literal, so neither half can smuggle
    a non-ASCII character back in.

  * THE PERSONA SELECT OWNS ITS ROW.  Settings' "How you use Systemu" dropdown
    rendered at its intrinsic width inside a full-width card, clipping the
    longest persona name.  Width is CSS, so there is no behaviour to assert -
    this is a source pin, and it is anchored to that one select by its label so
    that deleting the class from it reds even though the class is used
    elsewhere on the page.
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest

#: the label that identifies the persona dropdown in Settings' source
PERSONA_LABEL = "How you use Systemu"

#: the token that makes an input own its row (design/tokens.py: width 100%)
FULL_WIDTH_CLASS = "s-input-full"


# --- the finalize refusal ----------------------------------------------------
@pytest.fixture
def bare_provider_env(monkeypatch):
    """No provider credential from the developer's shell or an earlier test may
    reach the gate - otherwise it would pass and never produce a refusal."""
    from systemu.runtime import provider_status as ps
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    for i in (1, 2, 3):
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_PROVIDER", raising=False)
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


def _silent(_url, _timeout):
    """The keyless endpoint answers nothing, so nothing on this machine is
    usable and the gate must refuse.  Injected at the mint's own probe table,
    which is what keeps this test off the network."""
    from systemu.runtime import provider_status as ps
    return (ps.STATE_UNREACHABLE, "nothing answered (test)")


def _unusable_machine():
    from systemu.runtime import provider_status as ps
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    base.update({f"tier{i}_model": "deepseek/deepseek-v4-flash"
                 for i in (1, 2, 3)})
    base.update(output_dir="", non_interactive=False, vault_dir="/tmp/v",
                ollama_url="http://localhost:11434")
    return SimpleNamespace(**base)


def _refusal(monkeypatch) -> str:
    from systemu.runtime import provider_status as ps
    from systemu.interface.pages import welcome
    monkeypatch.setitem(ps._PROBES, "ollama", _silent)
    ok, msg = welcome.finalize_onboarding(
        None, _unusable_machine(), name="", refresh_key_fn=lambda c: False)
    assert ok is False, "the gate must refuse a machine with nothing usable"
    return msg


def test_the_finalize_refusal_is_pure_ascii(bare_provider_env, monkeypatch):
    msg = _refusal(monkeypatch)
    assert msg.isascii(), repr(msg)


def test_the_finalize_refusal_still_names_the_step_and_the_remedies(
        bare_provider_env, monkeypatch):
    """The dash swap is cosmetic: the sentence must still point at step 1 and
    still list the remedies, so this is not a copy regression in disguise."""
    msg = _refusal(monkeypatch)
    assert "provider" in msg.lower()
    assert "(step 1)" in msg
    assert "OPENROUTER_API_KEY" in msg


# --- the persona select width ------------------------------------------------
def _persona_select_classes() -> str:
    """The classes string on the ONE `ui.select` in Settings labelled with the
    persona question.  Raises (rather than returning nothing) if that select or
    its `.classes(...)` call is gone - a pin that silently found nothing would
    be no pin at all."""
    from systemu.interface.pages import settings
    tree = ast.parse(inspect.getsource(settings.build_settings_page))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "classes"):
            continue
        inner = node.func.value
        if not (isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "select"):
            continue
        labelled = any(
            kw.arg == "label" and isinstance(kw.value, ast.Constant)
            and kw.value.value == PERSONA_LABEL
            for kw in inner.keywords)
        if labelled:
            return " ".join(a.value for a in node.args
                            if isinstance(a, ast.Constant)
                            and isinstance(a.value, str))
    raise AssertionError(
        f"no ui.select(label={PERSONA_LABEL!r}).classes(...) in "
        "build_settings_page - the persona switcher lost its width pin")


def test_the_persona_select_carries_the_full_width_class():
    classes = _persona_select_classes().split()
    assert FULL_WIDTH_CLASS in classes, classes
    assert "s-input" in classes, classes


def test_the_full_width_class_is_a_real_token_not_a_typo():
    """A class name that no stylesheet defines would pass a source pin and
    still render narrow."""
    from systemu.interface.design import tokens
    css = inspect.getsource(tokens)
    assert f".{FULL_WIDTH_CLASS} " in css, (
        f"{FULL_WIDTH_CLASS} must be defined in the design tokens")
