"""— Stage 1 emits trace warnings on low confidence."""
from __future__ import annotations

from unittest.mock import MagicMock

from systemu.core.models import Scroll


def _scroll() -> Scroll:
    return Scroll(
        id="scroll_t2", name="Test", source_session_id="cap_x",
        raw_instructions_path="", narrative_md="",
    )


def test_low_confidence_appends_warn_event():
    """The CLI hook should append a warn-level event when intent is unusable."""
    from sharing_on.cli import _append_intent_trace
    s = _scroll()
    intent = MagicMock(is_usable=False, confidence="low",
                       intent="x", error=None)
    _append_intent_trace(s, intent)
    assert len(s.pipeline_trace) == 1
    e = s.pipeline_trace[0]
    assert e.stage == "intent"
    assert e.level == "warn"
    assert "low" in e.message.lower() or "fallback" in e.message.lower()
    assert e.detail.get("confidence") == "low"


def test_usable_confidence_appends_info_event():
    from sharing_on.cli import _append_intent_trace
    s = _scroll()
    intent = MagicMock(is_usable=True, confidence="medium",
                       intent="Track weather", error=None)
    _append_intent_trace(s, intent)
    assert len(s.pipeline_trace) == 1
    e = s.pipeline_trace[0]
    assert e.stage == "intent"
    assert e.level == "info"


def test_extraction_error_appends_error_event():
    from sharing_on.cli import _append_intent_trace
    s = _scroll()
    intent = MagicMock(is_usable=False, confidence="low",
                       intent="", error="API timeout")
    _append_intent_trace(s, intent)
    e = s.pipeline_trace[0]
    assert e.level == "error"
    assert "API timeout" in e.detail.get("error", "")
