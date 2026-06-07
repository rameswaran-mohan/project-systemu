import pytest
from unittest.mock import MagicMock
from systemu.runtime.memory_consolidator import MemoryConsolidator


class FakeVault:
    def __init__(self, tools=None, shadow=None):
        self._tools = {t["id"]: t for t in (tools or [])}
        self._shadow = shadow
    def find_tool(self, tid):
        t = self._tools.get(tid)
        if t is None: return None
        m = MagicMock()
        for k, v in t.items(): setattr(m, k, v)
        return m
    def find_shadow(self, sid):
        return self._shadow
    def save_shadow(self, sh):
        self._saved = sh


def test_empty_log_returns_empty_string():
    assert MemoryConsolidator().consolidate([], FakeVault()) == ""


def test_keeps_all_successes_verbatim():
    log = [
        {"status": "success", "tool": "fetch_json", "summary": "got weather data"},
        {"status": "success", "tool": "create_word_doc", "summary": "wrote file"},
    ]
    out = MemoryConsolidator().consolidate(log, FakeVault())
    assert "got weather data" in out
    assert "wrote file" in out


def test_drops_failures_whose_root_cause_resolved():
    log = [{"status": "failed", "tool": "fetch_json", "tool_id": "t1",
            "reason": "not enabled"}]
    vault = FakeVault([{"id": "t1", "enabled": True, "dry_run_status": "passed"}])
    out = MemoryConsolidator().consolidate(log, vault)
    assert "not enabled" not in out


def test_keeps_failures_whose_cause_still_present():
    log = [{"status": "failed", "tool": "fetch_json", "tool_id": "t1",
            "reason": "not enabled"}]
    vault = FakeVault([{"id": "t1", "enabled": False, "dry_run_status": "passed"}])
    out = MemoryConsolidator().consolidate(log, vault)
    assert "not enabled" in out


def test_drops_dep_pending_when_dry_run_now_passes():
    log = [{"status": "failed", "tool": "fetch_json", "tool_id": "t1",
            "reason": "DEP_PENDING: missing requests"}]
    vault = FakeVault([{"id": "t1", "enabled": True, "dry_run_status": "passed"}])
    out = MemoryConsolidator().consolidate(log, vault)
    assert "DEP_PENDING" not in out


def test_keeps_dep_pending_when_dry_run_still_failed():
    log = [{"status": "failed", "tool": "fetch_json", "tool_id": "t1",
            "reason": "DEP_PENDING: missing requests"}]
    vault = FakeVault([{"id": "t1", "enabled": True, "dry_run_status": "failed"}])
    out = MemoryConsolidator().consolidate(log, vault)
    assert "DEP_PENDING" in out


def test_caps_repeated_failures_at_n2():
    log = [{"status": "failed", "tool": "fetch_json", "tool_id": "t1",
            "reason": "timeout"} for _ in range(10)]
    vault = FakeVault([{"id": "t1", "enabled": False, "dry_run_status": None}])
    out = MemoryConsolidator(max_repeats=2).consolidate(log, vault)
    assert out.count("timeout") <= 3   # 2 verbatim + maybe 1 in aggregate text
    assert "8 more" in out


def test_aggregate_line_when_caps_applied():
    log = [{"status": "failed", "tool": "x", "tool_id": "tx",
            "reason": "boom"} for _ in range(5)]
    vault = FakeVault([{"id": "tx", "enabled": False}])
    out = MemoryConsolidator(max_repeats=2).consolidate(log, vault)
    assert "3 more" in out


def test_unknown_tool_id_treated_as_live_failure():
    log = [{"status": "failed", "tool": "ghost", "tool_id": "missing",
            "reason": "boom"}]
    out = MemoryConsolidator().consolidate(log, FakeVault())
    assert "boom" in out


def test_no_tool_id_in_entry_treated_as_live():
    log = [{"status": "failed", "tool": "ghost", "reason": "boom"}]
    out = MemoryConsolidator().consolidate(log, FakeVault())
    assert "boom" in out


def test_mixed_success_and_failure_order_preserved():
    log = [
        {"status": "success", "tool": "fetch_json", "summary": "ok"},
        {"status": "failed", "tool": "fetch_json", "tool_id": "t1",
         "reason": "rate-limited"},
        {"status": "success", "tool": "create_word_doc", "summary": "wrote"},
    ]
    vault = FakeVault([{"id": "t1", "enabled": False}])
    out = MemoryConsolidator().consolidate(log, vault)
    assert out.index("ok") < out.index("rate-limited") < out.index("wrote")


def test_non_dict_entries_ignored():
    log = ["random string", None, {"status": "success", "tool": "x", "summary": "ok"}]
    out = MemoryConsolidator().consolidate(log, FakeVault())
    assert "ok" in out


def test_reset_shadow_memory_keep_successes():
    """v0.6.8: prefers vault.get_shadow (Pydantic) over find_shadow (ORM row)
    because save_shadow expects Pydantic."""
    from systemu.runtime.memory_consolidator import reset_shadow_memory
    shadow = MagicMock()
    shadow.execution_log = [
        {"status": "success", "tool": "x", "summary": "ok"},
        {"status": "failed", "tool": "y", "reason": "boom"},
    ]
    vault = MagicMock()
    vault.get_shadow.return_value = shadow
    reset_shadow_memory(shadow_id="sh1", keep_successes=True, vault=vault)
    kept = [e["status"] for e in shadow.execution_log]
    assert kept == ["success"]
    vault.save_shadow.assert_called_once_with(shadow)


def test_reset_shadow_memory_full_wipe():
    from systemu.runtime.memory_consolidator import reset_shadow_memory
    shadow = MagicMock()
    shadow.execution_log = [{"status": "success"}, {"status": "failed"}]
    vault = MagicMock()
    vault.get_shadow.return_value = shadow
    reset_shadow_memory(shadow_id="sh1", keep_successes=False, vault=vault)
    assert shadow.execution_log == []


def test_reset_shadow_memory_unknown_shadow_noop():
    """get_shadow raising KeyError (Pydantic vault contract) is a no-op."""
    from systemu.runtime.memory_consolidator import reset_shadow_memory
    vault = MagicMock()
    vault.get_shadow.side_effect = KeyError("missing")
    # find_shadow shouldn't be reached either; ensure it returns None to be safe
    vault.find_shadow.return_value = None
    reset_shadow_memory(shadow_id="missing", keep_successes=True, vault=vault)
    vault.save_shadow.assert_not_called()


def test_reset_shadow_memory_falls_back_to_find_shadow():
    """If the vault only has find_shadow (test/orm vault), still works."""
    from systemu.runtime.memory_consolidator import reset_shadow_memory

    class FindOnlyVault:
        def __init__(self, shadow):
            self.shadow = shadow
            self.saved = None
        def find_shadow(self, _):
            return self.shadow
        def save_shadow(self, sh):
            self.saved = sh

    sh = MagicMock()
    sh.execution_log = [{"status": "success"}, {"status": "failed"}]
    vault = FindOnlyVault(sh)
    reset_shadow_memory(shadow_id="sh1", keep_successes=True, vault=vault)
    assert vault.saved is sh
    assert [e["status"] for e in sh.execution_log] == ["success"]
