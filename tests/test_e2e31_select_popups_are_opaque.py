"""e2e31 D-4: a popup menu is a SURFACE of this app, so this app must paint it.

THE WITNESSED DEFECT. On /welcome in Chrome the "How will you use Systemu?"
dropdown's five options were drawn straight over "Your name", "Location",
"Timezone" and their values -- the popup had no readable surface of its own.

THE CAUSE, from source. ``design/tokens.py`` carried exactly one rule for the
popup::

    .q-menu { border: 1px solid var(--color-border);
              box-shadow: 0 10px 30px rgba(0,0,0,.5) !important; }

It took the CHROME -- a token border, a custom shadow -- and left the FILL to
whatever the Quasar theme layer underneath happened to supply, while the same
stylesheet already forced ``body, html { color: var(--color-text) !important }``
which the portaled menu INHERITS (the menu is teleported to a bare div under
``body``, so no page container can reach it; the app stylesheet is the only
thing that can). Measured in the browser against this stylesheet: with Quasar's
``q-dark`` class on the menu the fill is ``rgb(29,29,29)``; with that class
absent -- light mode, an ``options-dark`` select, or any Quasar/NiceGUI change
to the classes QMenu emits -- the fill is ``rgb(255,255,255)`` under inherited
``rgb(233,235,245)`` text: the app's near-white option text on white. Owning
half a surface is what makes the other half a third party's decision.

THE PROPERTY pinned here:

    The popup surface is painted by this app's tokens, opaquely, in ONE place;
    no rule anywhere makes it transparent or translucent, and nothing the popup
    inherits from can fade it.

Pinned against the built stylesheet and the interface tree -- never a
screenshot, which cannot tell "opaque by accident" from "opaque by rule".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from systemu.interface.design.tokens import TOKENS, build_global_css

#: The one module allowed to style the popup surface.
_TOKENS_MODULE = Path("systemu/interface/design/tokens.py")
_INTERFACE = Path("systemu/interface")

#: Any of these in a background value means the surface is see-through to some
#: degree. A 6-digit hex and a bare ``var(--color-*)`` naming one are not.
_TRANSLUCENT = ("transparent", "rgba(", "hsla(", "color-mix(")

_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _rules(css: str):
    """``[(selector, declarations)]`` for the flat blocks of a stylesheet.

    Comments are stripped first: a ``/* ... */`` above a rule is part of the
    text between the previous ``}`` and the next ``{``, so leaving it in would
    make the selector unrecognisable and quietly turn every assertion below
    into a test of nothing.
    """
    return [(sel.strip(), body.strip())
            for sel, body in _RULE.findall(_COMMENT.sub("", css))]


def _declarations(body: str) -> dict:
    """``property -> value`` for one rule body; a later repeat wins, as in CSS."""
    out = {}
    for part in body.split(";"):
        if ":" not in part:
            continue
        prop, _, value = part.partition(":")
        out[prop.strip().lower()] = value.strip()
    return out


def _menu_rules(css: str):
    return [(sel, body) for sel, body in _rules(css) if ".q-menu" in sel]


@pytest.fixture
def css() -> str:
    return build_global_css()


def _eight_digit_hex(value: str) -> bool:
    return bool(re.search(r"#[0-9a-fA-F]{8}\b", value))


# ── the popup carries an opaque surface token ────────────────────────────────

def test_the_popup_surface_is_declared_at_all(css):
    """The bug in one assertion: the app styled the popup and never filled it."""
    base = [body for sel, body in _menu_rules(css)
            if sel.replace(" ", "") == ".q-menu"]
    assert base, "no app-level rule for the popup surface"
    decls = _declarations(base[-1])
    assert "background" in decls or "background-color" in decls, \
        ("the popup rule sets chrome (border/shadow) but no fill, so the "
         "surface is whatever the Quasar theme layer happens to supply")


def test_the_popup_surface_is_a_surface_token(css):
    base = [body for sel, body in _menu_rules(css)
            if sel.replace(" ", "") == ".q-menu"][-1]
    decls = _declarations(base)
    value = decls.get("background", decls.get("background-color", ""))
    assert "var(--color-surface" in value, \
        f"the popup fill must be a surface design token, got {value!r}"


def test_the_surface_token_the_popup_names_is_opaque():
    """Proof of opacity from source: the token is a 6-digit hex, which has no
    alpha channel -- not a screenshot that happened to look solid."""
    for name in ("surface", "surface2"):
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", TOKENS["color"][name]), \
            f"--color-{name} must be a fully opaque colour"


def test_the_popup_declares_its_own_text_colour(css):
    """The menu is portaled under ``body``, so it inherits the app's near-white
    text wherever it lands. Declaring the pair together is what makes the
    contrast a property of this stylesheet rather than of Quasar's."""
    base = [body for sel, body in _menu_rules(css)
            if sel.replace(" ", "") == ".q-menu"][-1]
    assert "color" in _declarations(base), \
        "the popup must name its own text colour beside its fill"


# ── nothing makes it see-through ─────────────────────────────────────────────

def test_no_rule_gives_the_popup_a_translucent_background(css):
    for sel, body in _menu_rules(css):
        decls = _declarations(body)
        for prop in ("background", "background-color"):
            value = decls.get(prop, "")
            if not value:
                continue
            for marker in _TRANSLUCENT:
                assert marker not in value, \
                    f"{sel} sets a see-through {prop}: {value!r}"
            assert not _eight_digit_hex(value), \
                f"{sel} sets an alpha hex {prop}: {value!r}"


def test_no_rule_fades_or_blurs_the_popup(css):
    for sel, body in _menu_rules(css):
        decls = _declarations(body)
        opacity = decls.get("opacity", "1")
        assert float(opacity.replace("!important", "").strip()) >= 1.0, \
            f"{sel} fades the popup: opacity {opacity}"
        assert "backdrop-filter" not in decls and "filter" not in decls, \
            f"{sel} filters the popup surface"


def test_nothing_the_popup_inherits_from_is_faded(css):
    """The other half of the finding. The popup is teleported to a bare div
    under ``body``, so ``body``/``html`` are the only ancestors it has -- and an
    ``opacity`` or ``backdrop-filter`` there would fade it whatever its own
    background says."""
    for sel, body in _rules(css):
        parts = {p.strip() for p in sel.split(",")}
        if not ({"body", "html"} & parts):
            continue
        decls = _declarations(body)
        assert "opacity" not in decls, f"{sel} fades everything the popup sits in"
        assert "backdrop-filter" not in decls, f"{sel} blurs it"


# ── one owner ────────────────────────────────────────────────────────────────

def test_only_the_token_stylesheet_styles_the_popup():
    """A second, page-level opinion about the popup surface is how one screen's
    dropdown ends up looking different from another's."""
    offenders = []
    for py in _INTERFACE.rglob("*.py"):
        if py == _TOKENS_MODULE or "__pycache__" in py.parts:
            continue
        if ".q-menu" in py.read_text(encoding="utf-8"):
            offenders.append(str(py).replace("\\", "/"))
    assert not offenders, \
        "the popup surface is owned by design/tokens.py only: " + ", ".join(offenders)
