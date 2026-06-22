import os
from unittest.mock import patch
from sharing_on.config import Config
from systemu.core.models import Objective, Tool, ToolType


class TestConfigFields:
    _V091_ENV_KEYS = (
        "SYSTEMU_VERIFIER_ENABLED",
        "SYSTEMU_VERIFIER_PER_TURN_CAP",
        "SYSTEMU_VERIFIER_REJECTION_BUDGET",
        "SYSTEMU_VERIFIER_MAX_CALLS_PER_RUN",
        "SYSTEMU_VERIFIER_TIER",
        "SYSTEMU_AUDIT_LOG_ENABLED",
        "SYSTEMU_STATE_DELTA_FILE_PREVIEW_CHARS",
        "SYSTEMU_STATE_DELTA_MAX_FILES_PER_SECTION",
    )

    def test_v0_9_1_defaults_match_spec(self, monkeypatch):
        for k in self._V091_ENV_KEYS:
            monkeypatch.delenv(k, raising=False)
        cfg = Config()
        assert cfg.verifier_enabled is True
        assert cfg.verifier_per_turn_cap == 2
        assert cfg.verifier_rejection_budget == 3
        assert cfg.verifier_max_calls_per_run == 50
        assert cfg.verifier_tier == 3  # v0.9.8 (B6): default 1 -> 3 (free, JSON-reliable)
        assert cfg.audit_log_enabled is True
        assert cfg.state_delta_file_preview_chars == 200
        assert cfg.state_delta_max_files_per_section == 50

    def test_env_var_overrides_via_from_env(self):
        env = {
            "SYSTEMU_VERIFIER_ENABLED": "false",
            "SYSTEMU_VERIFIER_PER_TURN_CAP": "1",
            "SYSTEMU_VERIFIER_REJECTION_BUDGET": "5",
            "SYSTEMU_VERIFIER_MAX_CALLS_PER_RUN": "20",
            "SYSTEMU_VERIFIER_TIER": "2",
            "SYSTEMU_AUDIT_LOG_ENABLED": "false",
            "SYSTEMU_STATE_DELTA_FILE_PREVIEW_CHARS": "500",
            "SYSTEMU_STATE_DELTA_MAX_FILES_PER_SECTION": "100",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = Config.from_env()
        assert cfg.verifier_enabled is False
        assert cfg.verifier_per_turn_cap == 1
        assert cfg.verifier_rejection_budget == 5
        assert cfg.verifier_max_calls_per_run == 20
        assert cfg.verifier_tier == 2
        assert cfg.audit_log_enabled is False
        assert cfg.state_delta_file_preview_chars == 500
        assert cfg.state_delta_max_files_per_section == 100



class TestObjectiveVerifierField:
    def test_verifier_defaults_none(self):
        obj = Objective(id=1, goal="g", success_criteria="ok")
        assert obj.verifier is None

    def test_verifier_set_to_string(self):
        obj = Objective(id=1, goal="g", success_criteria="ok",
                        verifier="File at /tmp/x exists")
        assert obj.verifier == "File at /tmp/x exists"

    def test_backward_compat_existing_scroll_dict_no_verifier_field(self):
        # Simulates a pre-v0.9.1 vault record: JSON encoded by older code
        # that didn't know about the verifier field. Must hydrate cleanly
        # with verifier=None — covers the on-disk-then-load path that the
        # default-constructor test doesn't exercise.
        json_blob = '{"id": 1, "goal": "g", "success_criteria": "ok"}'
        obj = Objective.model_validate_json(json_blob)
        assert obj.verifier is None


class TestToolToolsetField:
    def test_is_action_tool_defaults_false(self):
        tool = Tool(id="t1", name="read_file", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        assert tool.is_action_tool is False

    def test_toolset_defaults_none(self):
        tool = Tool(id="t1", name="read_file", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        assert tool.toolset is None

    def test_toolset_round_trip(self):
        tool = Tool(id="t1", name="read_file", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION, toolset="file")
        as_json = tool.model_dump_json()
        rebuilt = Tool.model_validate_json(as_json)
        assert rebuilt.toolset == "file"

    def test_is_action_tool_set_true(self):
        tool = Tool(id="t1", name="email_send", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    is_action_tool=True)
        assert tool.is_action_tool is True


class TestToolMaxResultSizeChars:
    def test_default_is_none_unbounded(self):
        tool = Tool(id="t1", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        assert tool.max_result_size_chars is None

    def test_set_to_integer(self):
        tool = Tool(id="t1", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    max_result_size_chars=100_000)
        assert tool.max_result_size_chars == 100_000


from pathlib import Path
import json
from systemu.vault.vault import Vault


class TestAuditLogFileBackend:
    def _make_vault(self, tmp_path: Path) -> Vault:
        return Vault(root=tmp_path)

    def test_append_writes_jsonl_line(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_action_audit({
            "ts": "2026-06-06T12:34:56Z",
            "user_id": "alice",
            "execution_id": "exec_a",
            "objective_id": 1,
            "action": "email.send",
            "params": {"to": "wife@example.com"},
            "success": True,
            "error": None,
        })
        audit_path = tmp_path / "audit" / "actions.jsonl"
        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "email.send"
        assert entry["execution_id"] == "exec_a"

    def test_append_appends_not_overwrites(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_action_audit({"ts": "t1", "execution_id": "e", "objective_id": 1,
                                "action": "a", "params": {}, "success": True, "error": None})
        v.append_action_audit({"ts": "t2", "execution_id": "e", "objective_id": 2,
                                "action": "b", "params": {}, "success": True, "error": None})
        lines = (tmp_path / "audit" / "actions.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2

    def test_query_filters_by_execution_id(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_action_audit({"ts": "t1", "execution_id": "e1", "objective_id": 1,
                                "action": "a", "params": {}, "success": True, "error": None})
        v.append_action_audit({"ts": "t2", "execution_id": "e2", "objective_id": 1,
                                "action": "b", "params": {}, "success": True, "error": None})
        rows = v.query_action_audit(execution_id="e1")
        assert len(rows) == 1
        assert rows[0]["action"] == "a"

    def test_query_returns_empty_when_no_audit_dir(self, tmp_path):
        v = self._make_vault(tmp_path)
        rows = v.query_action_audit(execution_id="anything")
        assert rows == []

    def test_query_filters_by_since_ts(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_action_audit({"ts": "2026-06-06T00:00:00Z", "execution_id": "e",
                                "objective_id": 1, "action": "a", "params": {},
                                "success": True, "error": None})
        v.append_action_audit({"ts": "2026-06-06T12:00:00Z", "execution_id": "e",
                                "objective_id": 1, "action": "b", "params": {},
                                "success": True, "error": None})
        rows = v.query_action_audit(execution_id="e", since_ts="2026-06-06T06:00:00Z")
        assert len(rows) == 1
        assert rows[0]["action"] == "b"

    def test_query_skips_malformed_lines_with_warning(self, tmp_path, caplog):
        """Defensive branch: corrupt JSONL line should be skipped + WARN,
        not raise. This is the ops-monitoring surface — keep covered."""
        import logging
        v = self._make_vault(tmp_path)
        # First append a valid entry so the audit dir + file exist.
        v.append_action_audit({
            "ts": "t1", "execution_id": "e", "objective_id": 1,
            "action": "good", "params": {}, "success": True, "error": None,
        })
        # Append a corrupt line directly.
        audit_path = tmp_path / "audit" / "actions.jsonl"
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write("not valid json\n")
        # Append another valid one.
        v.append_action_audit({
            "ts": "t2", "execution_id": "e", "objective_id": 1,
            "action": "good2", "params": {}, "success": True, "error": None,
        })
        with caplog.at_level(logging.WARNING):
            rows = v.query_action_audit(execution_id="e")
        # The two valid rows are returned, the corrupt line is silently skipped.
        actions = [r["action"] for r in rows]
        assert actions == ["good", "good2"]
        # And a WARNING was emitted for the malformed line.
        assert any("malformed audit line" in rec.message for rec in caplog.records)


from systemu.runtime import audit_log
from datetime import datetime, timezone


class TestAuditLogHelper:
    def test_append_action_writes_through_vault(self, tmp_path):
        v = Vault(root=tmp_path)
        audit_log.append_action(
            v,
            execution_id="exec_x",
            objective_id=2,
            action="write_csv_file",
            params={"path": "/tmp/out.csv"},
            success=True,
            error=None,
            user_id="alice",
        )
        rows = v.query_action_audit(execution_id="exec_x")
        assert len(rows) == 1
        assert rows[0]["action"] == "write_csv_file"
        assert rows[0]["user_id"] == "alice"
        assert rows[0]["params"] == {"path": "/tmp/out.csv"}

    def test_append_action_stamps_iso_timestamp(self, tmp_path):
        v = Vault(root=tmp_path)
        audit_log.append_action(v, execution_id="e", objective_id=1,
                                 action="a", params={}, success=True, error=None)
        rows = v.query_action_audit(execution_id="e")
        # Parse — raises on malformed ISO
        ts = rows[0]["ts"]
        datetime.fromisoformat(ts.replace("Z", "+00:00"))


import time
from systemu.runtime import state_delta


class TestStateDeltaCapture:
    def test_baseline_then_no_changes_yields_empty_delta(self, tmp_path):
        v = Vault(root=tmp_path)
        out_dir = tmp_path / "outputs"
        out_dir.mkdir()
        baseline = state_delta.capture_baseline(
            vault=v, execution_id="e", objective_id=1,
            default_output_dir=str(out_dir),
        )
        delta = state_delta.compute_delta(
            baseline=baseline, vault=v, default_output_dir=str(out_dir),
            chat_result=None, config=Config(),
        )
        assert delta.files_added == []
        assert delta.files_modified == []
        assert delta.audit_entries_added == []
        assert delta.chat_result_set is None

    def test_files_added_picked_up(self, tmp_path):
        v = Vault(root=tmp_path)
        out_dir = tmp_path / "outputs"
        out_dir.mkdir()
        baseline = state_delta.capture_baseline(
            vault=v, execution_id="e", objective_id=1,
            default_output_dir=str(out_dir),
        )
        # Avoid same-second mtime races
        time.sleep(0.01)
        (out_dir / "burritos.json").write_text('[{"name": "X"}]')
        delta = state_delta.compute_delta(
            baseline=baseline, vault=v, default_output_dir=str(out_dir),
            chat_result=None, config=Config(),
        )
        paths = [f["path"] for f in delta.files_added]
        assert any("burritos.json" in p for p in paths)

    def test_audit_entries_filter_by_execution_id(self, tmp_path):
        v = Vault(root=tmp_path)
        out_dir = tmp_path / "outputs"
        out_dir.mkdir()
        baseline = state_delta.capture_baseline(
            vault=v, execution_id="e1", objective_id=1,
            default_output_dir=str(out_dir),
        )
        from systemu.runtime import audit_log
        audit_log.append_action(v, execution_id="e1", objective_id=1,
                                 action="a", params={}, success=True, error=None)
        audit_log.append_action(v, execution_id="e2", objective_id=1,
                                 action="b", params={}, success=True, error=None)
        delta = state_delta.compute_delta(
            baseline=baseline, vault=v, default_output_dir=str(out_dir),
            chat_result=None, config=Config(),
            execution_id="e1",
        )
        actions = [e["action"] for e in delta.audit_entries_added]
        assert "a" in actions and "b" not in actions

    def test_extensions_slot_preserved(self, tmp_path):
        v = Vault(root=tmp_path)
        out_dir = tmp_path / "outputs"
        out_dir.mkdir()
        baseline = state_delta.capture_baseline(
            vault=v, execution_id="e", objective_id=1,
            default_output_dir=str(out_dir),
        )
        delta = state_delta.compute_delta(
            baseline=baseline, vault=v, default_output_dir=str(out_dir),
            chat_result=None, config=Config(),
            extensions={"skills_used": ["burrito-recipe"]},
        )
        assert delta.extensions == {"skills_used": ["burrito-recipe"]}


class TestStateDeltaScoping:
    def test_max_files_per_section_caps_list(self, tmp_path):
        v = Vault(root=tmp_path)
        out_dir = tmp_path / "outputs"
        out_dir.mkdir()
        baseline = state_delta.capture_baseline(
            vault=v, execution_id="e", objective_id=1,
            default_output_dir=str(out_dir),
        )
        time.sleep(0.01)
        for i in range(60):
            (out_dir / f"f{i}.txt").write_text("x")
        cfg = Config()
        cfg.state_delta_max_files_per_section = 50
        delta = state_delta.compute_delta(
            baseline=baseline, vault=v, default_output_dir=str(out_dir),
            chat_result=None, config=cfg,
        )
        assert len(delta.files_added) == 50

    def test_file_preview_capped_to_config_chars(self, tmp_path):
        v = Vault(root=tmp_path)
        out_dir = tmp_path / "outputs"
        out_dir.mkdir()
        baseline = state_delta.capture_baseline(
            vault=v, execution_id="e", objective_id=1,
            default_output_dir=str(out_dir),
        )
        time.sleep(0.01)
        big = "A" * 5000
        (out_dir / "big.txt").write_text(big)
        cfg = Config()
        cfg.state_delta_file_preview_chars = 200
        delta = state_delta.compute_delta(
            baseline=baseline, vault=v, default_output_dir=str(out_dir),
            chat_result=None, config=cfg,
        )
        preview = next(f["preview"] for f in delta.files_added if "big.txt" in f["path"])
        assert len(preview) <= 200


from systemu.runtime import objective_verifier
from systemu.runtime.state_delta import StateDelta


class TestObjectiveVerifierRun:
    def _obj(self):
        return Objective(id=1, goal="g", success_criteria="ok",
                         verifier="A file at /tmp/x exists")

    def _delta(self, **overrides):
        kwargs = dict(files_added=[], files_modified=[],
                      audit_entries_added=[], chat_result_set=None,
                      vault_records_added=[], iteration_start_ts="t0",
                      extensions={})
        kwargs.update(overrides)
        return StateDelta(**kwargs)

    def test_verified_true_passes_through(self, monkeypatch):
        def fake_call(*, tier, system, user, config, max_tokens=None, temperature=None, **kw):
            return {"verified": True, "reason": "file exists"}
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json", fake_call)
        cfg = Config()
        result = objective_verifier.run(
            objective=self._obj(), delta=self._delta(), config=cfg)
        assert result["verified"] is True
        assert "exists" in result["reason"]

    def test_verified_false_propagates(self, monkeypatch):
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json",
            lambda **kw: {"verified": False, "reason": "no file at expected path"})
        result = objective_verifier.run(
            objective=self._obj(), delta=self._delta(), config=Config())
        assert result["verified"] is False
        assert "expected path" in result["reason"]

    def test_malformed_json_soft_passes(self, monkeypatch):
        # v0.9.8 (B5): malformed verifier output must SOFT-PASS, not reject —
        # blocking the user's task over our own parse failure caused max-iteration
        # loops (the goal-level verifier is the real backstop for the deliverable).
        def fake_call(**kw):
            return {"not_the_right_shape": True}
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json", fake_call)
        result = objective_verifier.run(
            objective=self._obj(), delta=self._delta(), config=Config())
        assert result["verified"] is True
        assert "unparsable" in result["reason"] or "malformed" in result["reason"]
        assert "soft-pass" in result["reason"]

    def test_llm_exception_soft_passes(self, monkeypatch):
        # v0.9.8 (B5): a verifier-LLM infra failure must SOFT-PASS, not reject.
        def fake_call(**kw):
            raise RuntimeError("network down")
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json", fake_call)
        result = objective_verifier.run(
            objective=self._obj(), delta=self._delta(), config=Config())
        assert result["verified"] is True
        assert "verifier unavailable" in result["reason"]
        assert "soft-pass" in result["reason"]

    def test_verifier_disabled_short_circuits_to_true(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json",
            lambda **kw: called.append(1) or {"verified": False, "reason": "x"})
        cfg = Config()
        cfg.verifier_enabled = False
        result = objective_verifier.run(
            objective=self._obj(), delta=self._delta(), config=cfg)
        assert result["verified"] is True
        assert called == []  # llm_call_json never invoked

    def test_no_verifier_hint_short_circuits_to_true(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json",
            lambda **kw: called.append(1) or {"verified": False, "reason": "x"})
        obj = Objective(id=1, goal="g", success_criteria="ok", verifier=None)
        result = objective_verifier.run(
            objective=obj, delta=self._delta(), config=Config())
        assert result["verified"] is True
        assert called == []

    def test_llm_call_uses_canonical_kwargs(self, monkeypatch):
        """Guard against signature drift: the call must pass tier, system,
        user, and config positionally-or-keyword (NOT 'prompt'). This is
        the bug T7 review caught — monkeypatch fakes that accept **kw
        cannot distinguish 'prompt=' from 'user=', so we assert here."""
        captured = {}

        def fake_call(*, tier, system, user, config, max_tokens=None, temperature=None, **rest):
            # If the real call used prompt=..., 'user' would be missing
            # and Python would have raised TypeError before reaching here.
            captured["tier"] = tier
            captured["has_system"] = bool(system)
            captured["user_is_json"] = isinstance(user, str) and user.startswith("{")
            captured["has_config"] = config is not None
            return {"verified": True, "reason": "ok"}

        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json", fake_call)
        cfg = Config()
        result = objective_verifier.run(
            objective=self._obj(), delta=self._delta(), config=cfg)
        assert result["verified"] is True
        assert captured["tier"] == 3  # v0.9.8 (B6): verifier_tier default 1 -> 3
        assert captured["has_system"] is True
        assert captured["user_is_json"] is True
        assert captured["has_config"] is True


from systemu.runtime.tool_sandbox import ToolSandbox, ToolResult


class TestSandboxAuditsActionTools:
    def test_action_tool_success_writes_audit(self, tmp_path, monkeypatch):
        v = Vault(root=tmp_path)
        tool = Tool(id="t1", name="email_send", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    is_action_tool=True)
        # ToolSandbox takes vault_root as the first positional arg; vault and
        # config are keyword-only extras added in T8.
        sandbox = ToolSandbox(vault_root=tmp_path, vault=v, config=Config())
        sandbox._after_successful_call(
            tool=tool,
            params={"to": "wife@example.com"},
            execution_id="exec_z",
            objective_id=2,
            user_id="alice",
        )
        rows = v.query_action_audit(execution_id="exec_z")
        assert len(rows) == 1
        assert rows[0]["action"] == "email_send"
        assert rows[0]["params"] == {"to": "wife@example.com"}

    def test_read_tool_does_not_write_audit(self, tmp_path):
        v = Vault(root=tmp_path)
        tool = Tool(id="t2", name="read_file", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    is_action_tool=False)
        sandbox = ToolSandbox(vault_root=tmp_path, vault=v, config=Config())
        sandbox._after_successful_call(
            tool=tool, params={"path": "/etc/hosts"},
            execution_id="exec_z", objective_id=1,
        )
        assert v.query_action_audit(execution_id="exec_z") == []

    def test_audit_log_disabled_skips_write(self, tmp_path):
        v = Vault(root=tmp_path)
        cfg = Config()
        cfg.audit_log_enabled = False
        tool = Tool(id="t1", name="email_send", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    is_action_tool=True)
        sandbox = ToolSandbox(vault_root=tmp_path, vault=v, config=cfg)
        sandbox._after_successful_call(
            tool=tool, params={}, execution_id="exec_z", objective_id=1)
        assert v.query_action_audit(execution_id="exec_z") == []


class TestSandboxTruncation:
    def test_short_result_unchanged(self):
        tool = Tool(id="t", name="shell", description="d",
                    tool_type=ToolType.CLI_COMMAND,
                    max_result_size_chars=100)
        # ToolResult uses stdout= directly (not output={"stdout": ...})
        result = ToolResult(success=True, stdout="hi", stderr="")
        from systemu.runtime.tool_sandbox import truncate_result
        truncated = truncate_result(result, tool)
        assert truncated.stdout == "hi"

    def test_long_result_truncated(self):
        tool = Tool(id="t", name="shell", description="d",
                    tool_type=ToolType.CLI_COMMAND,
                    max_result_size_chars=50)
        big = "A" * 1000
        result = ToolResult(success=True, stdout=big, stderr="")
        from systemu.runtime.tool_sandbox import truncate_result
        truncated = truncate_result(result, tool)
        assert len(truncated.stdout) <= 100  # generous bound: 50 + marker
        assert "truncated" in truncated.stdout.lower()

    def test_no_cap_no_truncation(self):
        tool = Tool(id="t", name="shell", description="d",
                    tool_type=ToolType.CLI_COMMAND,
                    max_result_size_chars=None)
        big = "A" * 1000
        result = ToolResult(success=True, stdout=big, stderr="")
        from systemu.runtime.tool_sandbox import truncate_result
        truncated = truncate_result(result, tool)
        assert truncated.stdout == big


from systemu.pipelines import scroll_refiner


class TestRefinerEmitsVerifiers:
    def test_refined_objectives_carry_verifier(self, tmp_path, monkeypatch):
        # Monkeypatch llm_call_json to return objectives carrying verifier strings,
        # as if Tier-1 followed the new prompt instructions.
        def fake_call(**kw):
            return {
                "title": "Find top burritos",
                "intent": "find top burritos",
                "narrative_md": "Find and rank burrito places.",
                "expected_outcome": "ranked markdown list",
                "objectives": [
                    {"id": 1, "goal": "search for burritos",
                     "success_criteria": "raw json with ≥5 entries",
                     "verifier": "A file at /tmp/burritos-raw.json exists with at least 5 entries"},
                    {"id": 2, "goal": "rank",
                     "success_criteria": "ranked markdown",
                     "verifier": "A file at /tmp/burritos-ranked.md exists, sorted by rating, ≥1KB"},
                ],
                "constraints": {}, "tags": [],
            }
        monkeypatch.setattr("systemu.pipelines.scroll_refiner.llm_call_json", fake_call)
        # v0.9.1 final-review hermetic fix: patch the router source too so the
        # fake_call wins regardless of thread-pool event-loop state left by
        # prior tests (e.g. the unawaited-coroutine residue from
        # test_memory_tier_contract / test_metrics_tracker that corrupts
        # _run_coroutine and bypasses the module-namespace monkeypatch).
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_call)
        # Also block the async path so no real network call can escape via
        # _run_coroutine even if a prior test left the thread pool in an
        # unexpected state.
        async def _fake_async(**kw):
            return fake_call(**kw)
        monkeypatch.setattr("systemu.core.llm_router.async_llm_call_json", _fake_async)
        # Prevent vault.load_global_memory from reading any prior-test vault
        # state that leaked into module-level singletons.
        monkeypatch.setattr(
            "systemu.vault.vault.Vault.load_global_memory",
            lambda self: "",
        )
        # Stub out the deferred-import singletons so we don't need a real daemon.
        monkeypatch.setattr(
            "systemu.pipelines.activity_extractor.init_pipeline",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "systemu.interface.notifications.set_vault",
            lambda *a, **k: None,
        )
        v = Vault(root=tmp_path)
        scroll = scroll_refiner.refine_from_text(
            prompt="find top burrito places near me",
            vault=v,
            config=Config(),
        )
        verifiers = [o.verifier for o in scroll.objectives]
        assert all(v_ is not None for v_ in verifiers)
        assert "/tmp/burritos-raw.json" in verifiers[0]


class TestScrollRefinerParsesVerifier:
    def test_raw_objective_dict_with_verifier_hydrates(self):
        raw = {"id": 1, "goal": "g", "success_criteria": "ok",
               "verifier": "a file exists"}
        obj = Objective.model_validate(raw)
        assert obj.verifier == "a file exists"

    def test_raw_objective_dict_without_verifier_is_none(self):
        raw = {"id": 1, "goal": "g", "success_criteria": "ok"}
        obj = Objective.model_validate(raw)
        assert obj.verifier is None


# ---------------------------------------------------------------------------
# Task 11 (v0.9.1): fact_extractor SHA256 fingerprint short-circuit
# ---------------------------------------------------------------------------
from systemu.pipelines import fact_extractor  # noqa: E402


def _make_vault(tmp_path):
    """Bootstrap a minimal vault in tmp_path (mirrors TestFactExtractor._vault)."""
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions", "elder"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


class TestFactExtractorFingerprint:
    def test_unchanged_fingerprint_skips_llm(self, tmp_path, monkeypatch):
        """Calling extract_from_chat twice with the same chat_entry should only
        invoke the LLM once — the second call is short-circuited by the
        SHA256 fingerprint guard."""
        v = _make_vault(tmp_path)
        called = {"n": 0}

        def fake_call(**kw):
            called["n"] += 1
            return {"facts": []}

        monkeypatch.setattr(fact_extractor, "llm_call_json", fake_call)

        entry = {"ts": "2026-06-06T00:00:00", "prompt": "hi there", "status": "completed"}
        fact_extractor.extract_from_chat(entry, v, Config())
        fact_extractor.extract_from_chat(entry, v, Config())
        assert called["n"] == 1  # second call short-circuited

    def test_changed_fingerprint_runs_llm(self, tmp_path, monkeypatch):
        """Calling extract_from_chat with two different chat entries should
        invoke the LLM twice — the fingerprint changes between calls."""
        v = _make_vault(tmp_path)
        called = {"n": 0}

        def fake_call(**kw):
            called["n"] += 1
            return {"facts": []}

        monkeypatch.setattr(fact_extractor, "llm_call_json", fake_call)

        e1 = {"ts": "2026-06-06T00:00:00", "prompt": "hi there", "status": "completed"}
        e2 = {"ts": "2026-06-06T01:00:00", "prompt": "different prompt", "status": "completed"}
        fact_extractor.extract_from_chat(e1, v, Config())
        fact_extractor.extract_from_chat(e2, v, Config())
        assert called["n"] == 2


import json
from systemu.runtime import shadow_runtime as sr


# The runtime tests work against a thin harness that exposes only the
# completion-credit + verifier-call path. The implementer should add the
# v0.9.1 hook points (verifier_rejections, verifier_calls_this_turn,
# _objective_baselines) as attributes the harness can inspect.

class TestRuntimeCompletionRejection:
    def test_rejected_claim_does_not_credit(self, tmp_path, monkeypatch):
        v = Vault(root=tmp_path)
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json",
            lambda **kw: {"verified": False, "reason": "no file at /tmp/x"})
        cfg = Config()
        obj = Objective(id=1, goal="g", success_criteria="ok",
                        verifier="A file at /tmp/x exists")

        # Drive the completion-credit hook directly:
        outcome = sr.process_completion_claim(
            objective=obj, vault=v, config=cfg,
            execution_id="e", default_output_dir=str(tmp_path / "outputs"),
            chat_result=None,
            state=sr.ObjectiveState(rejection_count=0, calls_this_turn=0),
        )
        assert outcome.credited is False
        assert outcome.state.rejection_count == 1
        assert "no file" in outcome.feedback_message.lower()


class TestRuntimeRejectionBudget:
    def test_three_rejections_signals_stuck(self, monkeypatch, tmp_path):
        v = Vault(root=tmp_path)
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json",
            lambda **kw: {"verified": False, "reason": "x"})
        cfg = Config()
        obj = Objective(id=1, goal="g", success_criteria="ok",
                        verifier="needs file")
        state = sr.ObjectiveState(rejection_count=2, calls_this_turn=0)
        outcome = sr.process_completion_claim(
            objective=obj, vault=v, config=cfg,
            execution_id="e", default_output_dir=str(tmp_path / "outputs"),
            chat_result=None, state=state)
        assert outcome.state.rejection_count == 3
        assert outcome.escalate_stuck is True


class TestRuntimeFreshWorkGate:
    def test_per_turn_cap_blocks_extra_call(self, monkeypatch, tmp_path):
        v = Vault(root=tmp_path)
        called = {"n": 0}
        def fake_call(**kw):
            called["n"] += 1
            return {"verified": False, "reason": "still missing"}
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json", fake_call)
        cfg = Config()
        cfg.verifier_per_turn_cap = 2
        obj = Objective(id=1, goal="g", success_criteria="ok", verifier="x")
        state = sr.ObjectiveState(rejection_count=0, calls_this_turn=2)
        outcome = sr.process_completion_claim(
            objective=obj, vault=v, config=cfg,
            execution_id="e", default_output_dir=str(tmp_path / "outputs"),
            chat_result=None, state=state, fresh_work_since_last_call=False)
        assert called["n"] == 0  # gate blocked the call
        assert outcome.credited is False
        assert outcome.bypassed_verifier is True

    def test_per_turn_cap_clears_when_fresh_work_landed(self, monkeypatch, tmp_path):
        v = Vault(root=tmp_path)
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json",
            lambda **kw: {"verified": True, "reason": "ok"})
        cfg = Config()
        cfg.verifier_per_turn_cap = 2
        obj = Objective(id=1, goal="g", success_criteria="ok", verifier="x")
        state = sr.ObjectiveState(rejection_count=0, calls_this_turn=2)
        outcome = sr.process_completion_claim(
            objective=obj, vault=v, config=cfg,
            execution_id="e", default_output_dir=str(tmp_path / "outputs"),
            chat_result=None, state=state, fresh_work_since_last_call=True)
        assert outcome.credited is True


class TestRuntimeResumeRecredits:
    def test_existing_evidence_credits_on_resume(self, monkeypatch, tmp_path):
        v = Vault(root=tmp_path)
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "burritos.json").write_text("[1,2,3,4,5]")
        monkeypatch.setattr(
            "systemu.runtime.objective_verifier.llm_call_json",
            lambda **kw: {"verified": True, "reason": "file at expected path exists"})
        cfg = Config()
        obj = Objective(id=1, goal="g", success_criteria="ok",
                        verifier="A file at outputs/burritos.json exists")
        outcome = sr.recredit_on_resume(
            objective=obj, vault=v, config=cfg,
            execution_id="e", default_output_dir=str(out))
        assert outcome.credited is True
