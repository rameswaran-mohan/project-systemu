"""Tests for v0.6.0-c — Stage 2 intent-aware refine_scroll.

Covers:
  * Scroll.expected_outcome round-trip through Pydantic + SQLite vault
  * Self-check retry loop: first call sets self_check_passed=false → second call fixes
  * expected_outcome propagates from LLM output to Scroll
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: in-memory SQLite vault

@pytest.fixture
def sqlite_vault(tmp_path):
    """Real SqliteVault on a tmp SQLite file with the v0.6.0 schema."""
    from systemu.storage.sqlite.vault import SqliteVault
    db_path = tmp_path / "systemu.db"
    url = f"sqlite:///{db_path}"

    # Bootstrap the schema (production uses Alembic; tests just create-all)
    from sqlalchemy import create_engine
    from systemu.storage.sqlite.models import Base
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    engine.dispose()

    return SqliteVault(url, memory_dir=tmp_path / "memory")


# ─────────────────────────────────────────────────────────────────────────────
# Scroll.expected_outcome round-trip

class TestExpectedOutcomeRoundTrip:
    def test_pydantic_default_empty_string(self):
        from systemu.core.models import Scroll
        s = Scroll(
            id="s1", name="t", source_session_id="x",
            raw_instructions_path="", narrative_md="",
        )
        assert s.expected_outcome == ""

    def test_pydantic_accepts_value(self):
        from systemu.core.models import Scroll
        s = Scroll(
            id="s2", name="t", source_session_id="x",
            raw_instructions_path="", narrative_md="",
            expected_outcome="a dated weather doc exists on disk",
        )
        assert s.expected_outcome == "a dated weather doc exists on disk"

    def test_sqlite_round_trip(self, sqlite_vault):
        from systemu.core.models import Scroll
        original = Scroll(
            id="s_sql", name="t", source_session_id="x",
            raw_instructions_path="", narrative_md="",
            intent="document weather",
            expected_outcome="report.md exists with weather rows",
        )
        sqlite_vault.save_scroll(original)
        loaded = sqlite_vault.get_scroll("s_sql")
        assert loaded.intent == "document weather"
        assert loaded.expected_outcome == "report.md exists with weather rows"

    def test_sqlite_legacy_row_defaults_empty(self, sqlite_vault):
        """A scroll saved without expected_outcome loads cleanly (legacy
        compat — expected_outcome column is nullable)."""
        from systemu.core.models import Scroll
        s = Scroll(
            id="s_legacy", name="t", source_session_id="x",
            raw_instructions_path="", narrative_md="",
            intent="legacy intent",
        )
        sqlite_vault.save_scroll(s)
        loaded = sqlite_vault.get_scroll("s_legacy")
        assert loaded.expected_outcome == ""


# ─────────────────────────────────────────────────────────────────────────────
# Self-check retry loop

class TestSelfCheckRetry:
    def _vault_with_session_files(self, tmp_path):
        """Create a session dir with instructions.md + session.json so
        refine_scroll can run.  Returns (vault, session_dir)."""
        sess = tmp_path / "captures" / "test_session"
        sess.mkdir(parents=True)
        (sess / "instructions.md").write_text(
            "## Intent\n\n- **Intent:** document weather\n\n# Body\n",
            encoding="utf-8",
        )
        (sess / "session.json").write_text(
            json.dumps({
                "name": "weather session",
                "session_id": "sess_test_v060c",
            }),
            encoding="utf-8",
        )

        from systemu.vault.vault import Vault
        for sub in ("scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "elder", "notifications"):
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
            if sub != "elder":
                (tmp_path / sub / "index.json").write_text("[]")
        (tmp_path / "global_memory.jsonl").write_text("")
        (tmp_path / "chat_history.jsonl").write_text("")
        return Vault(str(tmp_path)), sess

    def test_self_check_passes_on_first_call_no_retry(self, tmp_path, monkeypatch):
        vault, sess = self._vault_with_session_files(tmp_path)

        call_count = [0]

        def fake_llm(**kw):
            call_count[0] += 1
            return {
                "title": "Doc weather",
                "intent": "Document weather",
                "expected_outcome": "weather doc exists",
                "narrative_md": "x",
                "objectives": [
                    {"id": 1, "goal": "fetch weather",
                     "success_criteria": "data received",
                     "output_type": "data"},
                ],
                "constraints": {}, "observed_preferences": {},
                "tags": ["weather"],
                "self_check_passed": True,
                "self_check_notes": "",
            }

        monkeypatch.setattr(
            "systemu.pipelines.scroll_refiner.llm_call_json", fake_llm,
        )
        config = MagicMock()
        config.openrouter_api_key = "k"
        config.tier1_model = "t"
        config.non_interactive = False

        from systemu.pipelines.scroll_refiner import refine_scroll
        scroll = refine_scroll(sess, config, vault, auto_proceed=True)

        assert call_count[0] == 1   # no retry needed
        assert scroll.expected_outcome == "weather doc exists"
        assert scroll.objectives[0].goal == "fetch weather"

    def test_self_check_fails_then_retry_succeeds(self, tmp_path, monkeypatch):
        vault, sess = self._vault_with_session_files(tmp_path)

        call_count = [0]

        def fake_llm(**kw):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: produces objectives that the LLM itself flags as failing
                return {
                    "title": "Doc weather",
                    "intent": "Document weather data",
                    "expected_outcome": "doc exists",
                    "narrative_md": "x",
                    "objectives": [
                        {"id": 1, "goal": "open snipping tool",
                         "success_criteria": "tool opened",
                         "output_type": "side_effect"},
                    ],
                    "constraints": {}, "observed_preferences": {},
                    "tags": [],
                    "self_check_passed": False,
                    "self_check_notes": "Obj 1 is a GUI action, not an outcome",
                }
            # Retry: now produces outcome-oriented objectives
            return {
                "title": "Doc weather",
                "intent": "Document weather data",
                "expected_outcome": "doc exists",
                "narrative_md": "x",
                "objectives": [
                    {"id": 1, "goal": "fetch weather data",
                     "success_criteria": "data received",
                     "output_type": "data"},
                ],
                "constraints": {}, "observed_preferences": {},
                "tags": [],
                "self_check_passed": True,
                "self_check_notes": "",
            }

        monkeypatch.setattr(
            "systemu.pipelines.scroll_refiner.llm_call_json", fake_llm,
        )
        config = MagicMock()
        config.openrouter_api_key = "k"
        config.tier1_model = "t"
        config.non_interactive = False

        from systemu.pipelines.scroll_refiner import refine_scroll
        scroll = refine_scroll(sess, config, vault, auto_proceed=True)

        assert call_count[0] == 2   # retry happened
        # The retry result was used — not the first attempt
        assert scroll.objectives[0].goal == "fetch weather data"

    def test_self_check_fails_twice_keeps_first_result(self, tmp_path, monkeypatch):
        """When self-check fails twice, we keep the FIRST result (with
        passed=false flag) so the operator card can surface it."""
        vault, sess = self._vault_with_session_files(tmp_path)

        call_count = [0]

        def fake_llm(**kw):
            call_count[0] += 1
            return {
                "title": "T",
                "intent": "I", "expected_outcome": "EO",
                "narrative_md": "n",
                "objectives": [
                    {"id": 1, "goal": "gui step",
                     "success_criteria": "y",
                     "output_type": "side_effect"},
                ],
                "constraints": {}, "observed_preferences": {},
                "tags": [],
                "self_check_passed": False,
                "self_check_notes": "couldn't fix it",
            }

        monkeypatch.setattr(
            "systemu.pipelines.scroll_refiner.llm_call_json", fake_llm,
        )
        config = MagicMock()
        config.openrouter_api_key = "k"
        config.tier1_model = "t"
        config.non_interactive = False

        from systemu.pipelines.scroll_refiner import refine_scroll
        scroll = refine_scroll(sess, config, vault, auto_proceed=True)

        assert call_count[0] == 2   # retried once, then stopped
        # Scroll still saved with the (admittedly bad) objectives — the
        # operator card from Stage 6 will catch it downstream.
        assert scroll.objectives[0].goal == "gui step"
