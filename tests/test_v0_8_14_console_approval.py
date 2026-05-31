# tests/test_v0_8_14_console_approval.py
"""v0.8.14 — Console approval discoverability."""
import pytest


class TestClearChatHistory:
    def test_clear_truncates_history(self, tmp_path):
        from systemu.vault.vault import Vault
        v = Vault(vault_dir=tmp_path)
        v.append_chat_history({"ts": "2026-05-31T00:00:00", "prompt": "a", "status": "failure"})
        v.append_chat_history({"ts": "2026-05-31T00:01:00", "prompt": "b", "status": "success"})
        assert len(v.load_chat_history()) == 2
        v.clear_chat_history()
        assert v.load_chat_history() == []

    def test_clear_is_safe_when_empty(self, tmp_path):
        from systemu.vault.vault import Vault
        v = Vault(vault_dir=tmp_path)
        v.clear_chat_history()  # must not raise
        assert v.load_chat_history() == []


class TestStaleTerminalHelper:
    def test_marks_old_terminal_entries(self):
        from systemu.interface.pages.chat_page import _stale_terminal_ts
        entries = [
            {"ts": "2026-05-31T09:02:00", "status": "failure"},          # old terminal -> stale
            {"ts": "2026-05-31T09:05:00", "status": "success"},          # old terminal -> stale
            {"ts": "2026-05-31T09:18:00", "status": "waiting_on_tools"}, # newest, non-terminal -> not stale
        ]
        stale = _stale_terminal_ts(entries)
        assert "2026-05-31T09:02:00" in stale
        assert "2026-05-31T09:05:00" in stale
        assert "2026-05-31T09:18:00" not in stale

    def test_newest_terminal_not_marked(self):
        from systemu.interface.pages.chat_page import _stale_terminal_ts
        entries = [
            {"ts": "2026-05-31T09:02:00", "status": "running"},
            {"ts": "2026-05-31T09:18:00", "status": "success"},  # newest -> current, not stale
        ]
        assert _stale_terminal_ts(entries) == set()

    def test_empty(self):
        from systemu.interface.pages.chat_page import _stale_terminal_ts
        assert _stale_terminal_ts([]) == set()

    def test_failed_status_is_terminal(self):
        # direct_task writes "failed" (extraction/decision crash) AND "failure"
        # (runtime result); both must be treated as stale terminal states.
        from systemu.interface.pages.chat_page import _stale_terminal_ts
        entries = [
            {"ts": "2026-05-31T09:02:00", "status": "failed"},           # old terminal -> stale
            {"ts": "2026-05-31T09:18:00", "status": "waiting_on_tools"}, # newest -> not stale
        ]
        assert "2026-05-31T09:02:00" in _stale_terminal_ts(entries)


class TestForgeLink:
    def test_forge_link_carries_tool_id(self):
        from systemu.interface.components.pending_tools import _forge_link
        assert _forge_link("tool_7870fa78") == "/tools?forge=tool_7870fa78"


class TestToolsForgeParam:
    def test_build_tools_page_accepts_forge_tool_id(self):
        import inspect
        from systemu.interface.pages.tools import build_tools_page
        params = inspect.signature(build_tools_page).parameters
        assert "forge_tool_id" in params
        assert params["forge_tool_id"].default is None

    def test_page_tools_reads_forge_query_param(self):
        import inspect
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard.create_dashboard) if hasattr(dashboard, "create_dashboard") else inspect.getsource(dashboard)
        assert "forge" in src and "build_tools_page(" in src


class TestPendingApprovalsCount:
    def test_counts_proposed_tools_and_pending_deps(self, monkeypatch, tmp_path):
        import systemu.interface.pages.console as console
        # fake vault: 2 proposed tools
        class FakeVault:
            def load_index(self, kind):
                return [{"status": "proposed"}, {"status": "proposed"}, {"status": "deployed"}] if kind == "tools" else []
        # 1 pending dep via DepApprovalStore
        monkeypatch.setattr("systemu.runtime.dep_approvals.DepApprovalStore.list_pending",
                            lambda self: [{"package": "requests"}])
        n = console._pending_approvals_count(FakeVault())
        assert n == 3  # 2 proposed tools + 1 pending dep

    def test_zero_when_nothing_pending(self, monkeypatch):
        import systemu.interface.pages.console as console
        class FakeVault:
            def load_index(self, kind): return [{"status": "deployed"}]
        monkeypatch.setattr("systemu.runtime.dep_approvals.DepApprovalStore.list_pending", lambda self: [])
        assert console._pending_approvals_count(FakeVault()) == 0
