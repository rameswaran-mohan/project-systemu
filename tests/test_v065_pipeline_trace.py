"""— PipelineTrace + has_warnings index flag."""
from __future__ import annotations

import pytest

from systemu.core.models import Scroll, TraceEvent, ScrollStatus


def _scroll(**kwargs) -> Scroll:
    base = dict(
        id="scroll_t1",
        name="Test",
        source_session_id="cap_x",
        raw_instructions_path="",
        narrative_md="",
    )
    base.update(kwargs)
    return Scroll(**base)


class TestTraceEvent:
    def test_minimal_event_constructs(self):
        e = TraceEvent(stage="intent", level="warn", message="x")
        assert e.stage == "intent"
        assert e.level == "warn"
        assert e.message == "x"
        assert e.detail == {}
        assert e.ts is not None

    def test_detail_is_kwargs(self):
        e = TraceEvent(
            stage="refine", level="error", message="y",
            detail={"pattern": "screenshot"},
        )
        assert e.detail["pattern"] == "screenshot"

    def test_invalid_stage_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TraceEvent(stage="banana", level="warn", message="x")

    def test_invalid_level_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TraceEvent(stage="intent", level="frobnicate", message="x")


class TestScrollPipelineTrace:
    def test_default_empty(self):
        s = _scroll()
        assert s.pipeline_trace == []
        assert s.has_warnings is False

    def test_append_and_has_warnings_info_only(self):
        s = _scroll()
        s.pipeline_trace.append(
            TraceEvent(stage="intent", level="info", message="ok")
        )
        assert s.has_warnings is False  # info doesn't count

    def test_has_warnings_true_on_warn(self):
        s = _scroll()
        s.pipeline_trace.append(
            TraceEvent(stage="refine", level="warn", message="GUI verb")
        )
        assert s.has_warnings is True

    def test_has_warnings_true_on_error(self):
        s = _scroll()
        s.pipeline_trace.append(
            TraceEvent(stage="validate", level="error", message="blocked")
        )
        assert s.has_warnings is True


class TestScrollStatusEnum:
    def test_validator_blocked_value_exists(self):
        assert ScrollStatus.VALIDATOR_BLOCKED.value == "validator_blocked"
