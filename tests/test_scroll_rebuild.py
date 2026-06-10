"""Phase 6 Slice 6f — Workshop's last surface (the interactive Scrolls rebuild)
becomes an in-place dialog.

The Workshop ``_scrolls_tab`` rebuild UI (prompt textarea + the
``workshop_module.rebuild_scroll`` call + its notify/result handling) was lifted
into ``systemu.interface.scroll_rebuild`` so the Scrolls page can open it in-place
from the row ``✏️ Edit`` button instead of deep-linking to the dissolving
``/workshop`` route.

Same split-the-data-from-the-paint discipline as scroll_gate / entity_edit — the
NiceGUI dialog shell can't run headless, so these tests exercise:

  * the testable async applier (``apply_scroll_rebuild``) — that it calls the
    UNCHANGED ``rebuild_scroll`` pipeline with (scroll_id, prompt, config, vault)
    and fires ``on_saved`` after a successful rebuild;
  * the empty-prompt guard (no rebuild call, no on_saved);
  * the dialog opener importable with the documented signature.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


# ── testable async applier: the preserved rebuild path ───────────────────────

def test_apply_scroll_rebuild_invokes_pipeline_and_fires_on_saved():
    """apply_scroll_rebuild calls workshop_module.rebuild_scroll with the
    scroll id + prompt + config + vault (UNCHANGED pipeline) and runs on_saved
    after a successful rebuild."""
    import systemu.interface.scroll_rebuild as sr

    vault = MagicMock()
    config = SimpleNamespace()
    fired = []
    updated = SimpleNamespace(name="My Scroll")

    with patch.object(sr, "rebuild_scroll", new=AsyncMock(return_value=updated)) as reb:
        result = asyncio.run(sr.apply_scroll_rebuild(
            "scroll_abc", "Make it more formal", config, vault,
            on_saved=lambda: fired.append(True),
        ))

    # success returns the rebuilt scroll (truthy), not the empty-prompt sentinel
    assert result is updated
    reb.assert_awaited_once_with("scroll_abc", "Make it more formal", config, vault)
    assert fired == [True]


def test_apply_scroll_rebuild_noop_on_empty_prompt():
    """A blank prompt → no pipeline call, no on_saved, returns False."""
    import systemu.interface.scroll_rebuild as sr

    vault = MagicMock()
    config = SimpleNamespace()
    fired = []

    with patch.object(sr, "rebuild_scroll", new=AsyncMock()) as reb:
        ok = asyncio.run(sr.apply_scroll_rebuild(
            "scroll_abc", "   ", config, vault,
            on_saved=lambda: fired.append(True),
        ))

    assert ok is False
    reb.assert_not_awaited()
    assert fired == []


def test_apply_scroll_rebuild_propagates_value_error():
    """A pipeline ValueError (e.g. scroll not found / validation) surfaces to the
    caller — the dialog shell maps it to a negative notify — and on_saved does
    NOT fire."""
    import systemu.interface.scroll_rebuild as sr
    import pytest

    vault = MagicMock()
    config = SimpleNamespace()
    fired = []

    with patch.object(sr, "rebuild_scroll",
                      new=AsyncMock(side_effect=ValueError("nope"))):
        with pytest.raises(ValueError):
            asyncio.run(sr.apply_scroll_rebuild(
                "scroll_abc", "do it", config, vault,
                on_saved=lambda: fired.append(True),
            ))
    assert fired == []


# ── dialog opener importable with the documented signature ───────────────────

def test_scroll_rebuild_dialog_opener_importable():
    import inspect
    from systemu.interface import scroll_rebuild

    fn = getattr(scroll_rebuild, "open_scroll_rebuild_dialog")
    sig = inspect.signature(fn)
    assert "scroll_id" in sig.parameters
    assert "on_saved" in sig.parameters
    assert sig.parameters["on_saved"].default is None
