"""W4.4 — accessibility pass: visible keyboard focus + accessible nav names.

Pins the two concrete gaps fixed: the global stylesheet now ships a
:focus-visible ring (the Quasar flatten layer had stripped all focus outlines),
and the icon-only collapsed nav links carry aria-label + aria-current so screen
readers can navigate them. The live DOM presence is checked via the smoke
browser; these guard the source contract.
"""
from __future__ import annotations

import inspect


class TestFocusVisibleRing:
    def test_global_css_has_focus_visible_outline(self):
        from systemu.interface.design.tokens import build_global_css
        css = build_global_css()
        assert ":focus-visible" in css, "keyboard users need a visible focus ring"
        assert "outline" in css
        # Focus ring uses a palette token, not a raw hardcoded colour.
        assert "var(--color-accent2)" in css

    def test_focus_ring_covers_keyboard_targets(self):
        from systemu.interface.design.tokens import build_global_css
        css = build_global_css()
        for target in (".q-btn:focus-visible", "a:focus-visible", ".q-tab:focus-visible"):
            assert target in css, f"{target} must get a focus outline"


class TestNavAccessibleNames:
    def test_nav_links_get_aria_label_and_current(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard._build_layout)
        # The nav <a> must carry an accessible name + active-state semantics.
        assert 'aria-label="{label}"' in src
        assert 'aria-current="page"' in src

    def test_hamburger_toggle_already_labelled(self):
        # Regression guard: the narrow-viewport sidebar toggle keeps its label.
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard._build_layout)
        assert 'aria-label="Toggle navigation"' in src
