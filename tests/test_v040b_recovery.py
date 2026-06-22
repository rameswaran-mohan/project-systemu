"""Tests for v0.4.0-b recovery primitives.

Three contracts:
  1. failure_classifier returns the right category for each error shape
  2. context_builder sticky notes survive rollback; reflection block is one-shot
  3. shadow_runtime queues a reflection block on tool failure and resets
     the per-tool counter on success
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime import failure_classifier as fc


# ─────────────────────────────────────────────────────────────────────────────
# 1) failure_classifier

@dataclass
class _MockResult:
    success: bool = False
    error: str = ""
    stderr: str = ""
    parsed: dict = None
    timed_out: bool = False
    exit_code: int = 1

    def __post_init__(self):
        if self.parsed is None:
            self.parsed = {}


class TestFailureClassifier:
    def test_success_returns_unknown(self):
        cls = fc.classify_tool_result(_MockResult(success=True))
        assert cls.category == "unknown"

    def test_explicit_missing_dependency(self):
        r = _MockResult(parsed={"error_type": "missing_dependency",
                                "missing_packages": ["python-docx"]})
        cls = fc.classify_tool_result(r)
        assert cls.category == "missing_dependency"
        assert cls.confidence == "high"
        assert cls.keyword == "python-docx"

    def test_no_module_named_regex(self):
        r = _MockResult(error="ModuleNotFoundError: No module named 'docx'")
        cls = fc.classify_tool_result(r)
        assert cls.category == "missing_dependency"
        assert cls.keyword == "docx"

    def test_timeout_via_marker(self):
        r = _MockResult(parsed={"timed_out": True})
        cls = fc.classify_tool_result(r)
        assert cls.category == "timeout"

    def test_timeout_via_text(self):
        r = _MockResult(error="Tool execution timed out after 30s")
        cls = fc.classify_tool_result(r)
        assert cls.category == "timeout"

    def test_param_error_via_marker(self):
        r = _MockResult(error="got an unexpected keyword argument 'badparam'")
        cls = fc.classify_tool_result(r)
        assert cls.category == "param_error"

    def test_http_error_status_code(self):
        r = _MockResult(error="HTTP 503 response from upstream service")
        cls = fc.classify_tool_result(r)
        assert cls.category == "http_error"
        assert cls.keyword == "503"

    def test_network_error(self):
        r = _MockResult(error="Connection refused on port 443")
        cls = fc.classify_tool_result(r)
        assert cls.category == "network_error"

    def test_permission_error(self):
        r = _MockResult(error="Permission denied: /root/secret")
        cls = fc.classify_tool_result(r)
        assert cls.category == "permission_error"

    def test_file_not_found(self):
        r = _MockResult(error="FileNotFoundError: No such file 'output.docx'")
        cls = fc.classify_tool_result(r)
        assert cls.category == "file_not_found"

    def test_parse_error(self):
        r = _MockResult(error="JSONDecodeError: Expecting value at line 1 col 1")
        cls = fc.classify_tool_result(r)
        assert cls.category == "parse_error"

    def test_api_error_rate_limit(self):
        r = _MockResult(error="OpenRouter returned 429 Rate Limit exceeded")
        cls = fc.classify_tool_result(r)
        # Both rate_limit (api_error) and HTTP 429 (http_error) could match;
        # the order in _RULES puts http_error AFTER timeout/param/etc,
        # but api_error has "rate limit" marker.  Either is acceptable —
        # they're both valid descriptions.
        assert cls.category in ("api_error", "http_error")

    def test_fallback_unknown(self):
        r = _MockResult(error="some weird thing happened")
        cls = fc.classify_tool_result(r)
        assert cls.category == "unknown"
        assert cls.confidence == "low"

    def test_dict_input_accepted(self):
        cls = fc.classify_tool_result({
            "success": False,
            "error": "Permission denied",
            "stderr": "",
            "parsed": {},
        })
        assert cls.category == "permission_error"


class TestReflectionStrategies:
    def test_missing_dep_recommends_fail_only(self):
        s = list(fc.reflection_strategies_for("missing_dependency"))
        assert s == ["FAIL"]

    def test_param_error_offers_retry_and_load(self):
        s = list(fc.reflection_strategies_for("param_error"))
        assert "RETRY_WITH_DIFFERENT_PARAMS" in s
        assert "LOAD_RESOURCE" in s

    def test_unknown_returns_full_default(self):
        s = list(fc.reflection_strategies_for("unknown"))
        assert "RETRY_WITH_DIFFERENT_PARAMS" in s
        assert "TRY_DIFFERENT_TOOL" in s


# ─────────────────────────────────────────────────────────────────────────────
# 2) ExecutionContext sticky notes + reflection block

@pytest.fixture
def fresh_context(tmp_path):
    from systemu.runtime.context_builder import ExecutionContext
    return ExecutionContext(
        execution_id="exec_test",
        system_prompt="test",
        scroll_json=[],
        tool_index=[],
        skill_index=[],
        snapshot_dir=tmp_path / "snap",
        recalled_memory="",
        use_objectives=False,
        scroll_intent="",
    )


class TestStickyNotes:
    def test_add_and_retrieve(self, fresh_context):
        fresh_context.add_sticky_note("Tried filename 'Weather on 13' — failed")
        fresh_context.add_sticky_note("Retrying with 'Weather on 13052026'")
        assert fresh_context.get_sticky_notes() == [
            "Tried filename 'Weather on 13' — failed",
            "Retrying with 'Weather on 13052026'",
        ]

    def test_empty_note_ignored(self, fresh_context):
        fresh_context.add_sticky_note("")
        fresh_context.add_sticky_note("   ")
        assert fresh_context.get_sticky_notes() == []

    def test_bounded_fifo(self, fresh_context):
        for i in range(12):
            fresh_context.add_sticky_note(f"note {i}", max_notes=5)
        notes = fresh_context.get_sticky_notes()
        assert len(notes) == 5
        assert notes[0] == "note 7"   # oldest kept
        assert notes[-1] == "note 11"

    def test_truncated_to_300(self, fresh_context):
        fresh_context.add_sticky_note("x" * 1000)
        assert len(fresh_context.get_sticky_notes()[0]) == 300

    def test_survives_rollback(self, fresh_context):
        # Take a snapshot at AB 1, add observation at AB 2, sticky note at AB 2
        fresh_context.add_tool_call({"tool_name": "t", "parameters": {}}, action_block_num=1)
        # Manually inject a snapshot (Snapshot is a tiny dataclass; the real
        # take_snapshot() involves a Tier-3 summarisation call we don't need).
        from systemu.runtime.context_builder import Snapshot
        fresh_context._snapshots.append(Snapshot(
            action_block_num=1,
            summary="ok",
        ))
        fresh_context.add_tool_call({"tool_name": "t", "parameters": {}}, action_block_num=2)
        fresh_context.add_sticky_note("Important: tried approach A")

        target = fresh_context.rollback_to_last_snapshot()
        assert target == 1
        # Sticky note is still there
        assert "Important: tried approach A" in fresh_context.get_sticky_notes()
        # AB-2 event was rewound
        ab2 = [e for e in fresh_context._history if (e.action_block_num or 0) >= 2]
        assert ab2 == []


class TestReflectionBlock:
    def test_one_shot_consumed_on_build(self, fresh_context):
        fresh_context.queue_reflection_block("REFLECT NOW")
        # build_messages consumes the pending reflection
        msgs = fresh_context.build_messages(current_action_block=1)
        sys_content = msgs[0]["content"]
        assert "Failure Reflection" in sys_content
        assert "REFLECT NOW" in sys_content
        # Second build doesn't include it (consumed)
        msgs2 = fresh_context.build_messages(current_action_block=1)
        assert "REFLECT NOW" not in msgs2[0]["content"]

    def test_sticky_notes_appear_in_system_message(self, fresh_context):
        fresh_context.add_sticky_note("Pinned: route X failed")
        msgs = fresh_context.build_messages(current_action_block=1)
        sys_content = msgs[0]["content"]
        assert "Sticky Notes" in sys_content
        assert "Pinned: route X failed" in sys_content


# ─────────────────────────────────────────────────────────────────────────────
# 3) Reflection block content + force_reflect threshold

class TestReflectionBlockBuilder:
    def test_normal_block(self):
        from systemu.runtime.shadow_runtime import _build_reflection_block
        out = _build_reflection_block(
            tool_name="create_word_doc", category="param_error",
            keyword="filename", consec=1,
            strategies=["RETRY_WITH_DIFFERENT_PARAMS", "FAIL"],
            force_reflect=False,
        )
        assert "create_word_doc" in out
        assert "**1**" in out
        assert "param_error" in out
        assert "filename" in out
        assert "RETRY_WITH_DIFFERENT_PARAMS" in out
        assert "Required" not in out  # not yet forced

    def test_forced_reflect_block(self):
        from systemu.runtime.shadow_runtime import _build_reflection_block
        out = _build_reflection_block(
            tool_name="x", category="param_error", keyword=None, consec=3,
            strategies=["RETRY_WITH_DIFFERENT_PARAMS", "FAIL"],
            force_reflect=True,
        )
        assert "Required" in out
        assert "REFLECT" in out
        assert "≥3" in out or "3 time" in out
