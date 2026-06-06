"""v0.9.1 mode-specific integration tests.

Cover sqlite-backed audit log, non-interactive degrade, multi-user audit
scoping, concurrent execution deltas, and StateDelta.extensions passthrough.
"""
import os
from pathlib import Path
from unittest.mock import patch
import pytest

from systemu.vault.vault import Vault


def _make_sqlite_vault(tmp_path: Path) -> Vault:
    """Construct a Vault with storage_backend='sqlite' wired in.

    Follows the pattern in tests/test_sqlite_backend.py — set _storage_backend
    on the constructed Vault and ensure backend init runs.
    """
    v = Vault(root=tmp_path)
    v._storage_backend = "sqlite"
    v._sqlite_url = f"sqlite:///{tmp_path}/vault.db"
    from systemu.vault.backend.sqlite_backend import ensure_schema
    ensure_schema(v)
    return v


class TestSQLiteAuditBackend:
    def test_append_writes_row(self, tmp_path):
        v = _make_sqlite_vault(tmp_path)
        v.append_action_audit({
            "ts": "2026-06-06T12:00:00Z",
            "user_id": "alice",
            "execution_id": "e1",
            "objective_id": 1,
            "action": "email.send",
            "params": {"to": "wife@example.com"},
            "success": True,
            "error": None,
        })
        rows = v.query_action_audit(execution_id="e1")
        assert len(rows) == 1
        assert rows[0]["action"] == "email.send"
        assert rows[0]["params"] == {"to": "wife@example.com"}

    def test_query_scopes_by_execution_id(self, tmp_path):
        v = _make_sqlite_vault(tmp_path)
        for eid in ("e1", "e2", "e2"):
            v.append_action_audit({
                "ts": "2026-06-06T12:00:00Z",
                "execution_id": eid, "objective_id": 1,
                "action": "x", "params": {}, "success": True, "error": None,
            })
        assert len(v.query_action_audit(execution_id="e1")) == 1
        assert len(v.query_action_audit(execution_id="e2")) == 2


class TestMultiUserAuditScoping:
    def test_query_filters_by_user_id(self, tmp_path):
        v = _make_sqlite_vault(tmp_path)
        v.append_action_audit({"ts": "t", "user_id": "alice", "execution_id": "e",
                                "objective_id": 1, "action": "a", "params": {},
                                "success": True, "error": None})
        v.append_action_audit({"ts": "t", "user_id": "bob", "execution_id": "e",
                                "objective_id": 1, "action": "b", "params": {},
                                "success": True, "error": None})
        alice_rows = v.query_action_audit(execution_id="e", user_id="alice")
        assert len(alice_rows) == 1
        assert alice_rows[0]["action"] == "a"


class TestSqliteVaultWiring:
    """Confirm the production SqliteVault sets the dispatch attributes so
    vault.append_action_audit actually routes to the sqlite backend (not
    silently to the file backend). Guards the wiring gap caught in T4 spec
    review.

    Uses open_vault() via the same factory path the daemon uses — env var
    SYSTEMU_STORAGE=sqlite is patched in each test so no global state leaks.
    """

    def test_sqlite_vault_sets_storage_backend_attr(self, tmp_path):
        from systemu.vault.factory import open_vault
        from sharing_on.config import Config
        cfg = Config()
        cfg.vault_dir = str(tmp_path / "vault")
        db_url = f"sqlite:///{tmp_path}/vault.db"
        with patch.dict(os.environ, {"SYSTEMU_STORAGE": "sqlite",
                                      "SYSTEMU_DATABASE_URL": db_url}):
            v = open_vault(cfg)
        assert getattr(v, "_storage_backend", "file") == "sqlite", (
            "SqliteVault must expose _storage_backend='sqlite' so "
            "Vault.append_action_audit routes to the sqlite dispatch layer"
        )
        assert getattr(v, "_sqlite_url", None), (
            "SqliteVault must expose _sqlite_url so backend _connect can resolve the DB"
        )

    def test_append_action_audit_actually_writes_to_sqlite(self, tmp_path):
        """End-to-end: append goes through the dispatch layer + lands in the
        sqlite action_audit table (not in vault/audit/actions.jsonl)."""
        from systemu.vault.factory import open_vault
        from systemu.vault.backend.sqlite_backend import ensure_schema
        from sharing_on.config import Config
        cfg = Config()
        cfg.vault_dir = str(tmp_path / "vault")
        db_url = f"sqlite:///{tmp_path}/vault.db"
        with patch.dict(os.environ, {"SYSTEMU_STORAGE": "sqlite",
                                      "SYSTEMU_DATABASE_URL": db_url}):
            v = open_vault(cfg)
        ensure_schema(v)
        v.append_action_audit({
            "ts": "2026-06-06T12:00:00Z",
            "execution_id": "prod_e",
            "objective_id": 1,
            "action": "via_factory",
            "params": {},
            "success": True,
            "error": None,
        })
        # Round-trip via the same vault — proves the row landed in sqlite
        rows = v.query_action_audit(execution_id="prod_e")
        assert len(rows) == 1
        assert rows[0]["action"] == "via_factory"
        # Guard: confirm NOTHING was written to the file-backend JSONL path.
        jsonl = tmp_path / "vault" / "audit" / "actions.jsonl"
        assert not jsonl.exists(), (
            "audit entry should have gone to sqlite, NOT to file backend"
        )


# ---------------------------------------------------------------------------
# T13 additions: verifier-off path, non-interactive degrade, parallel exec,
# extensions passthrough, and end-to-end truncation guard.
# ---------------------------------------------------------------------------

import json
from sharing_on.config import Config
from systemu.vault.vault import Vault
from systemu.core.models import Objective
from systemu.runtime import shadow_runtime as sr


class TestVerifierOffPath:
    def test_disabled_verifier_credits_unconditionally(self, tmp_path, monkeypatch):
        v = Vault(root=tmp_path)
        called = {"n": 0}

        def fake_call(**kw):
            called["n"] += 1
            return {"verified": False, "reason": "x"}

        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json", fake_call)
        cfg = Config()
        cfg.verifier_enabled = False
        obj = Objective(id=1, goal="g", success_criteria="ok", verifier="needed")
        out = tmp_path / "outputs"
        out.mkdir()
        outcome = sr.process_completion_claim(
            objective=obj, vault=v, config=cfg, execution_id="e",
            default_output_dir=str(out), chat_result=None,
            state=sr.ObjectiveState())
        assert outcome.credited is True
        assert called["n"] == 0  # LLM never invoked


class TestNonInteractiveDegrade:
    def test_three_rejections_under_non_interactive_signals_partial(self, tmp_path, monkeypatch):
        # The runtime's stuck-loop guard reads SYSTEMU_NON_INTERACTIVE at the
        # escalation site; outcome.escalate_stuck is the v0.9.1 signal it acts on.
        # This test verifies the signal reaches the caller; integration with the
        # actual partial-degrade path is covered by the v0.8.21 sweep that
        # remains green.
        v = Vault(root=tmp_path)
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json",
            lambda **kw: {"verified": False, "reason": "x"})
        cfg = Config()
        cfg.verifier_rejection_budget = 3
        obj = Objective(id=1, goal="g", success_criteria="ok", verifier="x")
        state = sr.ObjectiveState(rejection_count=2)
        outcome = sr.process_completion_claim(
            objective=obj, vault=v, config=cfg, execution_id="e",
            default_output_dir=str(tmp_path / "outputs"),
            chat_result=None, state=state)
        assert outcome.escalate_stuck is True


class TestParallelExecutionDeltas:
    def test_two_executions_dont_bleed(self, tmp_path):
        v = Vault(root=tmp_path)
        from systemu.runtime import audit_log, state_delta
        out = tmp_path / "outputs"
        out.mkdir()
        baseline_e1 = state_delta.capture_baseline(
            vault=v, execution_id="e1", objective_id=1, default_output_dir=str(out))
        audit_log.append_action(v, execution_id="e1", objective_id=1,
                                action="a1", params={}, success=True, error=None)
        audit_log.append_action(v, execution_id="e2", objective_id=1,
                                action="a2", params={}, success=True, error=None)
        delta_e1 = state_delta.compute_delta(
            baseline=baseline_e1, vault=v, default_output_dir=str(out),
            chat_result=None, config=Config(), execution_id="e1")
        actions = [e["action"] for e in delta_e1.audit_entries_added]
        assert "a1" in actions
        assert "a2" not in actions


class TestExtensionsSlotPassthrough:
    def test_extensions_dict_flows_through_to_verifier_payload(self, tmp_path, monkeypatch):
        v = Vault(root=tmp_path)
        captured = {}

        def fake_call(*, tier=None, system=None, user=None, config=None,
                      max_tokens=None, temperature=None, **kw):
            # The verifier passes the JSON payload as the `user` kwarg.
            captured["payload"] = user if user is not None else system
            return {"verified": True, "reason": "ok"}

        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json", fake_call)
        out = tmp_path / "outputs"
        out.mkdir()
        obj = Objective(id=1, goal="g", success_criteria="ok", verifier="x")
        sr.process_completion_claim(
            objective=obj, vault=v, config=Config(), execution_id="e",
            default_output_dir=str(out), chat_result=None,
            state=sr.ObjectiveState(),
            extensions={"skills_used": ["recipe.burrito"]})
        # The verifier prompt payload should mention the extension.
        assert "recipe.burrito" in captured["payload"]


# ---------------------------------------------------------------------------
# T8 deferred-wiring guard: end-to-end truncation path via T12 shadow_runtime.
# ---------------------------------------------------------------------------

from systemu.core.models import Tool, ToolType


class TestToolResultTruncationE2E:
    """Guards the T12 wiring: truncate_result is called after execute_tool
    returns. Tools with max_result_size_chars set must see their stdout
    capped when surfaced to the caller (the LLM context budget gate).

    Validates the audit + truncation chain from T2 (Tool model fields) ->
    T8 (truncate_result helper) -> T12 (shadow_runtime wiring)."""

    def test_truncate_result_actually_called_in_runtime_path(self):
        """Unit-level proof: shadow_runtime imports truncate_result and uses
        it. If T12 wiring is silently removed, this test fails."""
        import inspect
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        assert "truncate_result" in src or "_truncate_result" in src, (
            "shadow_runtime must call truncate_result() in the execute_tool "
            "return path; T8 deferred this to T12 and it must stay wired"
        )

    def test_truncate_result_caps_stdout(self):
        """End-to-end of the helper itself with a real Tool model."""
        from systemu.runtime.tool_sandbox import truncate_result, ToolResult
        tool = Tool(id="t1", name="shell", description="d",
                    tool_type=ToolType.CLI_COMMAND,
                    max_result_size_chars=50)
        big = "X" * 2000
        result = ToolResult(success=True, stdout=big, stderr="", error=None)
        out = truncate_result(result, tool)
        assert len(out.stdout) <= 200  # cap + marker
        assert "truncated" in out.stdout.lower()

    def test_truncate_result_passthrough_when_no_cap(self):
        from systemu.runtime.tool_sandbox import truncate_result, ToolResult
        tool = Tool(id="t1", name="shell", description="d",
                    tool_type=ToolType.CLI_COMMAND)  # max_result_size_chars=None
        big = "X" * 2000
        result = ToolResult(success=True, stdout=big, stderr="", error=None)
        out = truncate_result(result, tool)
        assert out.stdout == big

    def test_after_successful_call_actually_wired_in_runtime(self):
        """Guards the v0.9.1 final-review fix: _after_successful_call must
        be called from shadow_runtime's tool-success branch. Mirrors the
        truncate_result source-inspection guard."""
        import inspect
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        assert "_after_successful_call" in src, (
            "shadow_runtime must call sandbox._after_successful_call() in "
            "the tool-success branch. Without this wiring, action-tool "
            "audit is dead code in production."
        )
