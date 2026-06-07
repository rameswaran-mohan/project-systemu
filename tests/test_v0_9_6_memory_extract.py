"""v0.9.6 L7 memory consolidation tests — extends v0.9.1 fact_extractor."""
from pathlib import Path
from unittest.mock import patch
import pytest


class TestMemoryConsolidator:
    def test_consolidate_returns_facts_when_enabled(self, tmp_path, monkeypatch):
        from systemu.runtime.memory_consolidator import consolidate_run

        def fake_llm(**kwargs):
            return {
                "facts_learned": [
                    "user prefers spicy food",
                    "default shipping country is India",
                ],
                "patterns_observed": ["user often asks for ranked lists"],
            }
        monkeypatch.setattr(
            "systemu.runtime.memory_consolidator.llm_call_json", fake_llm,
        )
        from sharing_on.config import Config

        result = consolidate_run(
            chat_history=[
                {"role": "user", "content": "find spicy burritos"},
                {"role": "assistant", "content": "Done. Top 3 listed."},
            ],
            config=Config(),
        )
        assert result is not None
        assert "facts_learned" in result
        assert len(result["facts_learned"]) >= 1

    def test_consolidate_short_circuits_when_disabled(self, monkeypatch):
        from systemu.runtime.memory_consolidator import consolidate_run
        called = []
        monkeypatch.setattr(
            "systemu.runtime.memory_consolidator.llm_call_json",
            lambda **kw: called.append(1) or {},
        )
        from sharing_on.config import Config
        cfg = Config()
        cfg.memory_consolidation_enabled = False
        result = consolidate_run(
            chat_history=[{"role": "user", "content": "x"}],
            config=cfg,
        )
        assert result is None
        assert called == []

    def test_consolidate_handles_llm_failure(self, monkeypatch):
        from systemu.runtime.memory_consolidator import consolidate_run
        def boom(**kw):
            raise RuntimeError("LLM down")
        monkeypatch.setattr(
            "systemu.runtime.memory_consolidator.llm_call_json", boom,
        )
        from sharing_on.config import Config
        result = consolidate_run(
            chat_history=[{"role": "user", "content": "x"}],
            config=Config(),
        )
        assert result is None

    def test_dedup_by_fingerprint(self, tmp_path, monkeypatch):
        """Two consolidations on the same chat_history fingerprint -> 1 LLM call."""
        from systemu.runtime.memory_consolidator import consolidate_run
        called = []
        monkeypatch.setattr(
            "systemu.runtime.memory_consolidator.llm_call_json",
            lambda **kw: called.append(1) or {"facts_learned": [], "patterns_observed": []},
        )
        from sharing_on.config import Config
        history = [{"role": "user", "content": "test"}]
        consolidate_run(chat_history=history, config=Config(), cache_root=tmp_path)
        consolidate_run(chat_history=history, config=Config(), cache_root=tmp_path)
        # Second call should short-circuit via fingerprint cache
        assert len(called) == 1


class TestConfigMemoryFields:
    def test_defaults(self, monkeypatch):
        for k in ("SYSTEMU_MEMORY_CONSOLIDATION_ENABLED",):
            monkeypatch.delenv(k, raising=False)
        from sharing_on.config import Config
        cfg = Config()
        assert cfg.memory_consolidation_enabled is True
