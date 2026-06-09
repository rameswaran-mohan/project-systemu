"""The only UI building blocks. Each composes the token CSS classes; none carry
raw hex or inline f-string styles. Class logic is split into pure ``_*_classes``
helpers so it is unit-testable without a NiceGUI runtime."""
from __future__ import annotations

from typing import Optional

from systemu.interface.design.tokens import TOKENS

_BTN_VARIANTS = ("primary", "ghost", "danger")


def _pill_classes(status: str) -> str:
    tok = TOKENS["status"].get((status or "").lower(), "muted")
    return f"s-pill s-pill--{tok}"


def _btn_classes(variant: str) -> str:
    if variant not in _BTN_VARIANTS:
        raise ValueError(f"unknown button variant {variant!r}; use one of {_BTN_VARIANTS}")
    return f"s-btn s-btn--{variant}"


def status_pill_html(status: str) -> str:
    """Class-only replacement for the legacy inline-styled status_badge_html."""
    return f'<span class="{_pill_classes(status)}">{status}</span>'


def status_pill(status: str):
    from nicegui import ui
    return ui.html(status_pill_html(status))


def card(*, classes: str = ""):
    from nicegui import ui
    return ui.element("div").classes(f"s-card {classes}".strip())


def button(label: str, *, variant: str = "primary", on_click=None, icon: Optional[str] = None):
    from nicegui import ui
    btn = ui.button(label, on_click=on_click).props("flat no-caps")
    if icon:
        btn.props(f'icon={icon}')
    return btn.classes(_btn_classes(variant))


def text_input(label: str = "", *, value: str = "", placeholder: str = ""):
    from nicegui import ui
    return ui.input(label=label, value=value, placeholder=placeholder).classes("s-input")


def tabs():
    from nicegui import ui
    return ui.tabs().classes("s-tabs")
