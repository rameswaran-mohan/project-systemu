"""W3.4 — targeted except:pass burn-down on decision/data paths.

A silent `except: pass` on a path that writes data or records telemetry hides
real failures: the operator sees "success" while a credential file was
unreadable, a deleted schedule still haunts the index, or a run's terminal
telemetry never made it to the flywheel. These tests pin the new behaviour:
the swallow is replaced by an observable log line, WITHOUT changing control
flow (the function still returns / continues exactly as before).

Scope is deliberately narrow — only genuine DATA_LOSS sites. Cosmetic UI
render-guards and documented best-effort swallows are intentionally left alone.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── Slice A: credential store (systemu/runtime/credentials/store.py) ──────────
class TestCredentialStoreObservability:
    def _store(self, tmp_path):
        from systemu.runtime.credentials.store import CredentialStore
        store = CredentialStore(base_dir=tmp_path)
        return store

    def test_read_file_corruption_is_logged_not_swallowed(self, tmp_path, caplog):
        store = self._store(tmp_path)
        store._keyring = None  # force the file backend
        (tmp_path / ".credentials.json").write_text("{ this is not json", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="systemu.runtime.credentials.store"):
            # Control flow preserved: corrupt file → behaves as "key absent".
            assert store.get("api_key") is None

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("credential" in r.getMessage().lower() for r in warnings), \
            "corrupt credential-file read must log a warning, not pass silently"

    def test_keyring_delete_failure_is_logged_and_file_side_still_deletes(self, tmp_path, caplog):
        store = self._store(tmp_path)

        class _BoomKeyring:
            def delete_password(self, *a):
                raise RuntimeError("keyring locked")

        store._keyring = _BoomKeyring()
        store._write_file({"api_key": "secret", "other": "keep"})

        with caplog.at_level(logging.WARNING, logger="systemu.runtime.credentials.store"):
            store.delete("api_key")

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("delete" in r.getMessage().lower() for r in warnings), \
            "keyring delete failure must log (consistent with get/set), not pass silently"
        # Control flow preserved: the file-side deletion still happened.
        assert store._read_file() == {"other": "keep"}


# ── Slice B: schedule registry (systemu/scheduler/schedule_registry.py) ───────
class TestScheduleDeleteObservability:
    def test_index_rewrite_failure_on_delete_is_logged(self, tmp_path, caplog):
        from systemu.scheduler import schedule_registry as sr

        vault = SimpleNamespace(root=tmp_path)
        sched_dir = tmp_path / "schedules"
        sched_dir.mkdir(parents=True, exist_ok=True)
        sid = "sched_abc"
        (sched_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
        # Corrupt index → json.loads raises inside the index-prune block.
        (sched_dir / "index.json").write_text("{ corrupt index", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="systemu.scheduler.schedule_registry"):
            removed = sr.delete_schedule(sid, vault)

        # Control flow preserved: the schedule file was unlinked, returns True.
        assert removed is True
        assert not (sched_dir / f"{sid}.json").exists()
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(sid in r.getMessage() or "index" in r.getMessage().lower()
                   for r in warnings), \
            "failing to prune a deleted schedule from the index must log (orphan risk)"


# ── Slice C: shadow_runtime terminal telemetry (_observe_best_effort) ─────────
class TestObserveBestEffort:
    def test_returns_callable_result_on_success(self):
        from systemu.runtime.shadow_runtime import _observe_best_effort
        assert _observe_best_effort("label", lambda: 42) == 42

    def test_logs_and_returns_none_on_failure(self, caplog):
        from systemu.runtime.shadow_runtime import _observe_best_effort

        def _boom():
            raise RuntimeError("telemetry sink down")

        with caplog.at_level(logging.WARNING, logger="systemu.runtime.shadow_runtime"):
            out = _observe_best_effort("stuck-loop telemetry", _boom)

        # Control flow preserved: swallowed (returns None, never raises)…
        assert out is None
        # …but now observable.
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("stuck-loop telemetry" in r.getMessage() for r in warnings), \
            "best-effort failure must be logged with its label, not passed silently"
        assert any(r.exc_info for r in warnings), \
            "best-effort failure log should include the traceback (exc_info)"


# ── Slice D: shadow_decision dangling capability refs (decision context) ──────
class TestCreateShadowDanglingCapabilityRefs:
    """An activity that references a skill/tool id which no longer exists in the
    vault used to silently drop it from the shadow-persona LLM context. That's a
    data-integrity signal (a dangling required-capability reference) — it must
    now be logged, while the shadow is still created (control flow preserved)."""

    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications", "executions"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_dangling_skill_and_tool_refs_are_logged(self, tmp_path, caplog, monkeypatch):
        from systemu.core.models import Activity
        from systemu.pipelines import shadow_decision as sd

        vault = self._vault(tmp_path)
        activity = Activity(
            id="act_1", name="Do the thing", scroll_id="scroll_gone",
            required_skill_ids=["skill_missing"], required_tool_ids=["tool_missing"],
        )
        # Keep the test off the network / off AppState.
        monkeypatch.setattr(sd, "llm_call_json",
                            lambda **k: {"system_prompt": "p", "description": "d"})
        monkeypatch.setattr(sd, "notify_user", lambda **k: None)
        monkeypatch.setattr(sd, "log_event", lambda *a, **k: None)
        monkeypatch.setattr(sd, "_advance_scroll_after_shadow_assignment", lambda *a, **k: None)

        with caplog.at_level(logging.WARNING, logger="systemu.pipelines.shadow_decision"):
            shadow = sd.create_shadow(activity, "TestShadow", config=MagicMock(),
                                      vault=vault, skip_supervisor=True)

        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("skill_missing" in m for m in msgs), \
            f"dangling required-skill ref must be logged; got {msgs}"
        assert any("tool_missing" in m for m in msgs), \
            f"dangling required-tool ref must be logged; got {msgs}"
        # Control flow preserved: the shadow is still created, missing tool excluded.
        assert shadow is not None
        assert "tool_missing" not in shadow.available_tool_ids
