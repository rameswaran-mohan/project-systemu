"""W11.2 — composer-first chat layout.

Field report (2026-06-12): "The submit task chat page is not ux friendly.
I had to scroll down to chat." Root cause: build_chat_page rendered the
history column (up to 20 cards) ABOVE the mode control and composer, so
with any history the input was below the fold.

Contract: the composer (mode control + textarea + Run) is constructed
before the history column, the textarea autofocuses, and the keyboard
shortcut is discoverable.
"""
from __future__ import annotations

import inspect


def _chat_src() -> str:
    from systemu.interface.pages import chat_page
    return inspect.getsource(chat_page.build_chat_page)


class TestComposerFirst:
    def test_composer_constructed_before_history(self):
        src = _chat_src()
        composer_at = src.index("prompt_input = ui.textarea")
        history_at = src.index("history_col = ui.column")
        assert composer_at < history_at, \
            "the operator must never scroll past history to type a task"

    def test_mode_control_constructed_before_history(self):
        src = _chat_src()
        lane_at = src.index("lane = ui.radio")
        history_at = src.index("history_col = ui.column")
        assert lane_at < history_at

    def test_textarea_autofocuses(self):
        assert "autofocus" in _chat_src(), \
            "landing on Chat should put the cursor in the composer"

    def test_shortcut_is_discoverable(self):
        src = _chat_src()
        assert "Ctrl+Enter" in src, "the keyboard shortcut must be visible, not secret"

    def test_existing_contracts_hold(self):
        """W8.3 lane options and the prefill plumbing survive the reorder."""
        src = _chat_src()
        for needle in ('"quick"', '"run_now"', '"queue"', "prefill"):
            assert needle in src
