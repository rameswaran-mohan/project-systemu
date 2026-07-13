"""R-UX2 — the Live-events detail renderer bounds each block so one huge
tool_result / params / reasoning can't push a NiceGUI sync past the WebSocket
message limit ("Connection lost — message too long"). Pure clip logic, unit-tested.
"""
from __future__ import annotations

from systemu.interface.components.live_events_pane import clip_detail, _MAX_DETAIL_CHARS


def test_short_text_is_untouched():
    assert clip_detail("hello") == "hello"
    assert clip_detail("") == ""
    assert clip_detail(None) == ""                      # defensive


def test_long_text_is_clipped_with_a_note():
    big = "x" * (_MAX_DETAIL_CHARS + 5000)
    out = clip_detail(big)
    assert len(out) < len(big)
    assert out.startswith("x" * 100)                    # keeps the head
    assert "truncated" in out and "5,000 more chars" in out


def test_clip_is_deterministic_and_bounded():
    big = "y" * 200_000
    a, b = clip_detail(big), clip_detail(big)
    assert a == b                                       # deterministic
    # the rendered block is bounded near the cap (+ a short truncation note)
    assert len(a) <= _MAX_DETAIL_CHARS + 120
