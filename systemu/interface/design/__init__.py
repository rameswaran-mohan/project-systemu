"""Systemu UI design system — the single source of tokens, primitives, icons.

Import tokens/primitives/icons from here, never hand-style. Re-theming = editing
``tokens.py`` only. (Spec: docs/superpowers/specs/2026-06-08-ui-ux-revamp-design.md §4.4)
"""
from systemu.interface.design.tokens import TOKENS, build_global_css  # noqa: F401
from systemu.interface.design.icons import icon, ICONS  # noqa: F401,E402
from systemu.interface.design.primitives import (  # noqa: F401,E402
    status_pill, status_pill_html, card, button, text_input, tabs,
)
