"""v0.9.2 episodic memory tests."""
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import pytest

from sharing_on.config import Config
from systemu.core.models import SessionSummary
from systemu.vault.vault import Vault


class TestSessionSummaryModel:
    def _make(self, **overrides):
        kwargs = dict(
            id="session_summary_abc",
            session_id="sess_1",
            execution_id=None,
            user_id=None,
            started_at=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 6, 7, 12, 5, tzinfo=timezone.utc),
            status="success",
            intent="find burritos",
            outcome_summary="Listed 5 burrito places in Bangalore",
            key_facts_learned=["user lives in Bangalore"],
            files_produced=["/tmp/burritos.json"],
            tags=["food", "bangalore"],
            raw_chat_id=None,
        )
        kwargs.update(overrides)
        return SessionSummary(**kwargs)

    def test_minimal_construction(self):
        s = self._make()
        assert s.session_id == "sess_1"
        assert s.status == "success"

    def test_json_round_trip(self):
        s = self._make()
        rebuilt = SessionSummary.model_validate_json(s.model_dump_json())
        assert rebuilt.session_id == s.session_id
        assert rebuilt.tags == s.tags

    def test_defaults_for_optional(self):
        s = self._make(execution_id=None, user_id=None, raw_chat_id=None)
        assert s.execution_id is None
        assert s.user_id is None
        assert s.raw_chat_id is None


class TestConfigEpisodicFields:
    _KEYS = (
        "SYSTEMU_EPISODIC_MEMORY_ENABLED",
        "SYSTEMU_SUMMARIZE_AFTER_RUN",
        "SYSTEMU_EPISODIC_SEARCH_DEFAULT_LIMIT",
        "SYSTEMU_EPISODIC_SUMMARY_MAX_CHARS",
        "SYSTEMU_EPISODIC_TAGS_MAX_COUNT",
    )

    def test_defaults(self, monkeypatch):
        for k in self._KEYS:
            monkeypatch.delenv(k, raising=False)
        cfg = Config()
        assert cfg.episodic_memory_enabled is True
        assert cfg.summarize_after_run is True
        assert cfg.episodic_search_default_limit == 5
        assert cfg.episodic_summary_max_chars == 800
        assert cfg.episodic_tags_max_count == 8

    def test_env_overrides(self):
        env = {
            "SYSTEMU_EPISODIC_MEMORY_ENABLED": "false",
            "SYSTEMU_SUMMARIZE_AFTER_RUN": "false",
            "SYSTEMU_EPISODIC_SEARCH_DEFAULT_LIMIT": "10",
            "SYSTEMU_EPISODIC_SUMMARY_MAX_CHARS": "1500",
            "SYSTEMU_EPISODIC_TAGS_MAX_COUNT": "12",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = Config.from_env()
        assert cfg.episodic_memory_enabled is False
        assert cfg.summarize_after_run is False
        assert cfg.episodic_search_default_limit == 10
        assert cfg.episodic_summary_max_chars == 1500
        assert cfg.episodic_tags_max_count == 12


class TestSessionSummaryFileBackend:
    def _make_vault(self, tmp_path: Path) -> Vault:
        return Vault(root=tmp_path)

    def _make_summary(self, **overrides):
        kwargs = dict(
            id="ss_1", session_id="sess_1",
            started_at=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 6, 7, 12, 5, tzinfo=timezone.utc),
            status="success", intent="find burritos",
            outcome_summary="Listed 5 places",
            key_facts_learned=[], files_produced=[], tags=["food"],
        )
        kwargs.update(overrides)
        return SessionSummary(**kwargs)

    def test_append_creates_jsonl(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_session_summary(self._make_summary())
        path = tmp_path / "episodic" / "sessions.jsonl"
        assert path.exists()
        assert path.read_text().strip().count("\n") == 0  # one entry, no trailing blank

    def test_query_returns_in_append_order(self, tmp_path):
        v = self._make_vault(tmp_path)
        for i in range(3):
            v.append_session_summary(self._make_summary(id=f"ss_{i}", session_id=f"sess_{i}"))
        out = v.query_session_summaries(limit=10)
        assert [s.session_id for s in out] == ["sess_0", "sess_1", "sess_2"]

    def test_query_filters_by_user_id(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_session_summary(self._make_summary(id="ss_a", session_id="a", user_id="alice"))
        v.append_session_summary(self._make_summary(id="ss_b", session_id="b", user_id="bob"))
        out = v.query_session_summaries(user_id="alice", limit=10)
        assert [s.session_id for s in out] == ["a"]

    def test_query_filters_by_status(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_session_summary(self._make_summary(id="ss_1", session_id="a", status="success"))
        v.append_session_summary(self._make_summary(id="ss_2", session_id="b", status="failed"))
        out = v.query_session_summaries(status="failed", limit=10)
        assert [s.session_id for s in out] == ["b"]

    def test_query_filters_by_since_ts(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_session_summary(self._make_summary(
            id="ss_old", session_id="old",
            completed_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)))
        v.append_session_summary(self._make_summary(
            id="ss_new", session_id="new",
            completed_at=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)))
        out = v.query_session_summaries(
            since_ts=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
            limit=10)
        assert [s.session_id for s in out] == ["new"]

    def test_query_returns_empty_when_no_file(self, tmp_path):
        v = self._make_vault(tmp_path)
        out = v.query_session_summaries(limit=10)
        assert out == []

    def test_search_keyword_match_in_intent(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_session_summary(self._make_summary(id="ss_a", session_id="a", intent="find burrito places"))
        v.append_session_summary(self._make_summary(id="ss_b", session_id="b", intent="find ramen shops"))
        out = v.search_session_summaries("burrito", limit=5)
        assert [s.session_id for s in out] == ["a"]

    def test_search_keyword_match_in_tags(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_session_summary(self._make_summary(id="ss_a", session_id="a", tags=["food", "bangalore"]))
        v.append_session_summary(self._make_summary(id="ss_b", session_id="b", tags=["travel", "japan"]))
        out = v.search_session_summaries("bangalore", limit=5)
        assert [s.session_id for s in out] == ["a"]

    def test_search_case_insensitive(self, tmp_path):
        v = self._make_vault(tmp_path)
        v.append_session_summary(self._make_summary(id="ss_a", session_id="a", intent="Find BURRITO places"))
        out = v.search_session_summaries("burrito", limit=5)
        assert len(out) == 1


class TestEpisodicMemoryCapture:
    def test_capture_returns_summary(self, tmp_path, monkeypatch):
        from systemu.runtime import episodic_memory
        v = Vault(root=tmp_path)

        def fake_llm(**kw):
            return {
                "outcome_summary": "Ranked top 5 burrito places in Bangalore",
                "key_facts_learned": ["user is in Bangalore"],
                "tags": ["food", "bangalore"],
            }
        monkeypatch.setattr(
            "systemu.runtime.episodic_memory.llm_call_json", fake_llm)

        summary = episodic_memory.capture(
            vault=v,
            session_id="sess_test",
            intent="find burritos",
            chat_result="Done. See /tmp/burritos.json.",
            files_produced=["/tmp/burritos.json"],
            status="success",
            config=Config(),
        )
        assert summary is not None
        assert summary.session_id == "sess_test"
        assert summary.outcome_summary.startswith("Ranked")
        assert "bangalore" in [t.lower() for t in summary.tags]

    def test_capture_persists_to_vault(self, tmp_path, monkeypatch):
        from systemu.runtime import episodic_memory
        v = Vault(root=tmp_path)
        monkeypatch.setattr(
            "systemu.runtime.episodic_memory.llm_call_json",
            lambda **kw: {"outcome_summary": "ok",
                          "key_facts_learned": [], "tags": []})
        episodic_memory.capture(
            vault=v, session_id="sess_X", intent="x",
            chat_result=None, files_produced=[], status="success",
            config=Config())
        out = v.query_session_summaries(limit=10)
        assert len(out) == 1
        assert out[0].session_id == "sess_X"

    def test_capture_short_circuits_when_disabled(self, tmp_path, monkeypatch):
        from systemu.runtime import episodic_memory
        v = Vault(root=tmp_path)
        called = []
        monkeypatch.setattr(
            "systemu.runtime.episodic_memory.llm_call_json",
            lambda **kw: called.append(1) or {})
        cfg = Config()
        cfg.episodic_memory_enabled = False
        summary = episodic_memory.capture(
            vault=v, session_id="sess_Y", intent="x",
            chat_result=None, files_produced=[], status="success",
            config=cfg)
        assert summary is None
        assert called == []

    def test_capture_handles_llm_failure(self, tmp_path, monkeypatch):
        from systemu.runtime import episodic_memory
        v = Vault(root=tmp_path)
        def fake_llm(**kw):
            raise RuntimeError("LLM unavailable")
        monkeypatch.setattr(
            "systemu.runtime.episodic_memory.llm_call_json", fake_llm)
        summary = episodic_memory.capture(
            vault=v, session_id="sess_Z", intent="x",
            chat_result=None, files_produced=[], status="success",
            config=Config())
        assert summary is None

    def test_capture_skips_duplicate_session_id(self, tmp_path, monkeypatch):
        from systemu.runtime import episodic_memory
        v = Vault(root=tmp_path)
        monkeypatch.setattr(
            "systemu.runtime.episodic_memory.llm_call_json",
            lambda **kw: {"outcome_summary": "ok",
                          "key_facts_learned": [], "tags": []})
        episodic_memory.capture(
            vault=v, session_id="sess_dup", intent="x",
            chat_result=None, files_produced=[], status="success",
            config=Config())
        episodic_memory.capture(
            vault=v, session_id="sess_dup", intent="x",
            chat_result=None, files_produced=[], status="success",
            config=Config())
        out = v.query_session_summaries(limit=10)
        assert len(out) == 1


class TestSessionTools:
    def test_session_search_returns_list_of_dicts(self, tmp_path, monkeypatch):
        from systemu.runtime.tools.session_tools import session_search
        from systemu.runtime import episodic_memory
        v = Vault(root=tmp_path)
        monkeypatch.setattr(
            "systemu.runtime.episodic_memory.llm_call_json",
            lambda **kw: {"outcome_summary": "burrito ranking",
                          "key_facts_learned": [], "tags": ["food", "bangalore"]})
        episodic_memory.capture(
            vault=v, session_id="sess_burrito",
            intent="find burritos", chat_result=None,
            files_produced=[], status="success", config=Config())

        results = session_search(vault=v, query="burrito", limit=5)
        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["session_id"] == "sess_burrito"
        assert "intent" in results[0]
        assert "outcome_summary" in results[0]

    def test_session_recall_returns_full_summary(self, tmp_path, monkeypatch):
        from systemu.runtime.tools.session_tools import session_recall
        from systemu.runtime import episodic_memory
        v = Vault(root=tmp_path)
        monkeypatch.setattr(
            "systemu.runtime.episodic_memory.llm_call_json",
            lambda **kw: {"outcome_summary": "done",
                          "key_facts_learned": ["user prefers spicy"], "tags": ["food"]})
        episodic_memory.capture(
            vault=v, session_id="sess_recall",
            intent="x", chat_result=None,
            files_produced=[], status="success", config=Config())

        result = session_recall(vault=v, session_id="sess_recall")
        assert result is not None
        assert result["session_id"] == "sess_recall"
        assert "user prefers spicy" in result["key_facts_learned"]

    def test_session_recall_returns_none_when_not_found(self, tmp_path):
        from systemu.runtime.tools.session_tools import session_recall
        v = Vault(root=tmp_path)
        result = session_recall(vault=v, session_id="nonexistent")
        assert result is None


class TestRuntimeHooks:
    def test_shadow_runtime_calls_capture_on_finalize(self, tmp_path, monkeypatch):
        """When shadow_runtime finalizes a run, episodic_memory.capture is invoked."""
        captured = []
        def fake_capture(**kw):
            captured.append(kw)
            return None
        monkeypatch.setattr("systemu.runtime.episodic_memory.capture", fake_capture)
        from systemu.runtime.shadow_runtime import _trigger_episodic_capture
        v = Vault(root=tmp_path)
        _trigger_episodic_capture(
            vault=v, config=Config(),
            session_id="sess_h", intent="t",
            chat_result="done", files_produced=[],
            status="success", execution_id="e", user_id=None,
        )
        assert len(captured) == 1
        assert captured[0]["session_id"] == "sess_h"

    def test_capture_disabled_skips_hook(self, tmp_path, monkeypatch):
        captured = []
        monkeypatch.setattr(
            "systemu.runtime.episodic_memory.capture",
            lambda **kw: captured.append(kw))
        from systemu.runtime.shadow_runtime import _trigger_episodic_capture
        v = Vault(root=tmp_path)
        cfg = Config()
        cfg.summarize_after_run = False
        _trigger_episodic_capture(
            vault=v, config=cfg,
            session_id="sess_disabled", intent="t",
            chat_result=None, files_produced=[],
            status="success", execution_id=None, user_id=None,
        )
        assert captured == []

    def test_hook_swallows_capture_exception(self, tmp_path, monkeypatch):
        """A flaky LLM during summarization must NOT break the user's task."""
        def raising(**kw):
            raise RuntimeError("LLM is down")
        monkeypatch.setattr("systemu.runtime.episodic_memory.capture", raising)
        from systemu.runtime.shadow_runtime import _trigger_episodic_capture
        v = Vault(root=tmp_path)
        # Must NOT raise
        _trigger_episodic_capture(
            vault=v, config=Config(),
            session_id="sess_x", intent="t",
            chat_result=None, files_produced=[],
            status="success", execution_id=None, user_id=None,
        )


class TestCliSession:
    def _seed(self, tmp_path, monkeypatch):
        """Seed a vault with 2 session summaries with distinct intents/outcomes."""
        from systemu.runtime import episodic_memory
        v = Vault(root=tmp_path)

        def _smart_llm(**kw):
            prompt = kw.get("user", kw.get("prompt", ""))
            if "burrito" in prompt:
                return {"outcome_summary": "ranked 5 burrito places",
                        "key_facts_learned": ["user is in Bangalore"],
                        "tags": ["food", "bangalore"]}
            return {"outcome_summary": "found top ramen spots",
                    "key_facts_learned": ["user likes noodles"],
                    "tags": ["food", "ramen"]}

        monkeypatch.setattr("systemu.runtime.episodic_memory.llm_call_json", _smart_llm)
        episodic_memory.capture(
            vault=v, session_id="sess_burrito",
            intent="find burritos", chat_result=None,
            files_produced=["/tmp/burritos.json"],
            status="success", config=Config())
        episodic_memory.capture(
            vault=v, session_id="sess_ramen",
            intent="find ramen", chat_result=None,
            files_produced=[], status="success", config=Config())
        return v

    def test_session_list(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import session_cli
        self._seed(tmp_path, monkeypatch)
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        result = CliRunner().invoke(session_cli, ["list"])
        assert result.exit_code == 0
        assert "sess_burrito" in result.output
        assert "sess_ramen" in result.output

    def test_session_show(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import session_cli
        self._seed(tmp_path, monkeypatch)
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        result = CliRunner().invoke(session_cli, ["show", "sess_burrito"])
        assert result.exit_code == 0
        assert "find burritos" in result.output
        assert "bangalore" in result.output.lower()

    def test_session_search(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import session_cli
        self._seed(tmp_path, monkeypatch)
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        result = CliRunner().invoke(session_cli, ["search", "burrito"])
        assert result.exit_code == 0
        assert "sess_burrito" in result.output
        # Searching for "burrito" should not return the ramen session
        assert "sess_ramen" not in result.output
