"""v0.6.9: MemoryConsolidator.consolidate_with_buffer filters memory_buffer
lessons whose referenced tool is now healthy (cause resolved)."""
from unittest.mock import MagicMock
from systemu.runtime.memory_consolidator import MemoryConsolidator


class FakeVault:
    def __init__(self, tools=None):
        self._tools = {t["id"]: t for t in (tools or [])}
    def find_tool(self, tid):
        t = self._tools.get(tid)
        if t is None:
            return None
        m = MagicMock()
        for k, v in t.items():
            setattr(m, k, v)
        return m


def _lesson(tool_id, tool_name, category, text):
    return {
        "exec_id": "e1", "category": category, "lesson": text,
        "tool_id": tool_id, "tool_name": tool_name,
    }


def test_drops_buffer_lesson_when_tool_now_passes_dry_run():
    vault = FakeVault([{"id": "t1", "enabled": True, "dry_run_status": "passed"}])
    buffer = [_lesson("t1", "fetch_json", "failure_patterns",
                      "fetch_json fails on dependency")]
    out = MemoryConsolidator().consolidate_with_buffer(
        execution_log=[], buffer_entries=buffer, vault=vault,
    )
    assert "fetch_json fails" not in out


def test_keeps_buffer_lesson_when_tool_still_broken():
    vault = FakeVault([{"id": "t1", "enabled": False, "dry_run_status": "failed"}])
    buffer = [_lesson("t1", "fetch_json", "failure_patterns",
                      "fetch_json fails on dependency")]
    out = MemoryConsolidator().consolidate_with_buffer(
        execution_log=[], buffer_entries=buffer, vault=vault,
    )
    assert "fetch_json fails" in out


def test_keeps_observational_categories_unconditionally():
    """tool_quirks / domain_glossary / heuristics / self_assessment are
    observational and never filtered by tool health."""
    vault = FakeVault([{"id": "t1", "enabled": True, "dry_run_status": "passed"}])
    buffer = [_lesson("t1", "fetch_json", "tool_quirks",
                      "fetch_json returns 200 even on empty body")]
    out = MemoryConsolidator().consolidate_with_buffer(
        execution_log=[], buffer_entries=buffer, vault=vault,
    )
    assert "200 even on empty body" in out


def test_buffer_entries_appear_after_execution_log_in_view():
    vault = FakeVault([{"id": "t1", "enabled": False}])
    log = [{"status": "success", "tool": "x", "summary": "did the thing"}]
    buffer = [_lesson("t1", "fetch_json", "tool_quirks", "quirk observation")]
    out = MemoryConsolidator().consolidate_with_buffer(
        execution_log=log, buffer_entries=buffer, vault=vault,
    )
    assert out.index("did the thing") < out.index("quirk observation")


def test_empty_buffer_returns_execution_log_only():
    vault = FakeVault()
    log = [{"status": "success", "tool": "x", "summary": "ok"}]
    out = MemoryConsolidator().consolidate_with_buffer(
        execution_log=log, buffer_entries=[], vault=vault,
    )
    assert "ok" in out
    assert "## Lessons" not in out


def test_empty_both_returns_empty_string():
    vault = FakeVault()
    out = MemoryConsolidator().consolidate_with_buffer(
        execution_log=[], buffer_entries=[], vault=vault,
    )
    assert out == ""


def test_non_dict_buffer_entries_ignored():
    vault = FakeVault()
    out = MemoryConsolidator().consolidate_with_buffer(
        execution_log=[], buffer_entries=["junk", None, 42], vault=vault,
    )
    assert out == ""
