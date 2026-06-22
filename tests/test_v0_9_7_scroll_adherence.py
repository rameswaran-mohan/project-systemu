"""v0.9.7 Phase 3.3 — Scroll.raw_request + Scroll.adherence fields.

Decision 0.1 #2: the authoritative GOAL is the raw user message verbatim.
Per-SOP adherence is chosen at save time (free|guided|strict|None).

Tests:
  1. New fields round-trip through model_dump_json / model_validate_json.
  2. Scroll defaults: raw_request None, adherence None.
  3. Old-style Scroll JSON (without these fields) still validates (backward-compat).
  4. refine_from_text stores raw_request = the input prompt (LLM call mocked).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from systemu.core.models import Scroll, ScrollStatus


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _minimal_scroll(**overrides) -> Scroll:
    """Create the smallest valid Scroll for testing."""
    base = dict(
        id=f"scroll_{uuid.uuid4().hex[:8]}",
        name="Test SOP",
        source_session_id="sess_test",
        raw_instructions_path="",
        narrative_md="Do the thing.",
    )
    base.update(overrides)
    return Scroll(**base)


# ─── 1. Round-trip ──────────────────────────────────────────────────────────

def test_scroll_raw_request_and_adherence_round_trip():
    """New fields survive model_dump_json → model_validate_json unchanged."""
    scroll = _minimal_scroll(
        raw_request="Please export last month's sales data to CSV",
        adherence="strict",
    )
    json_str = scroll.model_dump_json()
    restored = Scroll.model_validate_json(json_str)

    assert restored.raw_request == "Please export last month's sales data to CSV"
    assert restored.adherence == "strict"


def test_scroll_adherence_all_valid_values():
    """All documented adherence strings are accepted (no Enum — free-form str)."""
    for level in ("free", "guided", "strict"):
        s = _minimal_scroll(adherence=level)
        assert s.adherence == level

    # None is also valid (default / system choice)
    s = _minimal_scroll(adherence=None)
    assert s.adherence is None


def test_scroll_round_trip_dict_mode():
    """model_dump(mode='json') / model_validate also works (not just the JSON str)."""
    scroll = _minimal_scroll(
        raw_request="Summarise Q4 report",
        adherence="guided",
    )
    data = scroll.model_dump(mode="json")
    assert data["raw_request"] == "Summarise Q4 report"
    assert data["adherence"] == "guided"

    restored = Scroll.model_validate(data)
    assert restored.raw_request == "Summarise Q4 report"
    assert restored.adherence == "guided"


# ─── 2. Defaults ────────────────────────────────────────────────────────────

def test_scroll_default_raw_request_is_none():
    scroll = _minimal_scroll()
    assert scroll.raw_request is None


def test_scroll_default_adherence_is_none():
    scroll = _minimal_scroll()
    assert scroll.adherence is None


# ─── 3. Backward-compatibility ──────────────────────────────────────────────

def test_old_scroll_json_without_new_fields_still_validates():
    """A pre-v0.9.7 Scroll dict (lacking raw_request / adherence) validates fine."""
    old_style = {
        "id": "scroll_abc123",
        "name": "Legacy SOP",
        "source_session_id": "sess_old",
        "raw_instructions_path": "/path/to/instructions.md",
        "narrative_md": "Old-style narrative.",
        "status": "approved",
        # Deliberately omitting raw_request and adherence
    }
    scroll = Scroll.model_validate(old_style)
    assert scroll.raw_request is None
    assert scroll.adherence is None
    assert scroll.name == "Legacy SOP"


def test_old_scroll_json_str_without_new_fields_still_validates():
    """Same check via model_validate_json path (what the vault uses on load)."""
    old_json = json.dumps({
        "id": "scroll_legacy",
        "name": "Old SOP",
        "source_session_id": "sess_legacy",
        "raw_instructions_path": "",
        "narrative_md": "Prose.",
        "intent": "Some intent",
        "status": "refined",
    })
    scroll = Scroll.model_validate_json(old_json)
    assert scroll.raw_request is None
    assert scroll.adherence is None
    assert scroll.id == "scroll_legacy"


# ─── 4. refine_from_text stores raw_request ─────────────────────────────────

def _fake_llm_result() -> dict[str, Any]:
    """Minimal LLM response that refine_from_text can parse without error."""
    return {
        "title": "Mocked Scroll",
        "narrative_md": "Do the mocked thing.",
        "intent": "Accomplish the mock",
        "expected_outcome": "Mock done",
        "objectives": [],
        "constraints": {},
        "observed_preferences": {},
        "tags": [],
    }


def test_refine_from_text_stores_raw_request(monkeypatch):
    """refine_from_text sets scroll.raw_request to the verbatim input prompt."""
    import systemu.pipelines.scroll_refiner as sr
    from systemu.vault.vault import Vault

    # Minimal vault stub — only the methods refine_from_text touches.
    # Use monkeypatch on the module-level names that are imported at the top
    # of scroll_refiner; init_pipeline and set_vault are deferred imports so
    # we patch them at their source modules instead.
    vault = MagicMock()
    vault.load_global_memory.return_value = ""
    vault.get_user_profile.return_value = None
    vault.load_user_facts.return_value = []
    vault.save_scroll.return_value = None

    config = MagicMock()

    captured_llm_calls = []

    def fake_llm(**kw):
        captured_llm_calls.append(kw)
        return _fake_llm_result()

    monkeypatch.setattr(sr, "llm_call_json", fake_llm)
    # init_pipeline / set_vault are deferred imports inside the function body;
    # patch them at their origin modules so the deferred import finds the stub.
    monkeypatch.setattr(
        "systemu.pipelines.activity_extractor.init_pipeline",
        lambda *a, **k: None,
        raising=False,
    )
    monkeypatch.setattr(
        "systemu.interface.notifications.set_vault",
        lambda *a, **k: None,
        raising=False,
    )

    raw_prompt = "Export all invoices from last quarter to a single PDF"
    scroll = sr.refine_from_text(raw_prompt, vault, config)

    # The verbatim user message must be stored on the scroll
    assert scroll.raw_request == raw_prompt
    # LLM was called exactly once (no clarifying-questions retry)
    assert len(captured_llm_calls) == 1


def test_refine_from_text_raw_request_equals_exact_prompt(monkeypatch):
    """raw_request is the exact string passed in — not trimmed or transformed."""
    import systemu.pipelines.scroll_refiner as sr

    vault = MagicMock()
    vault.load_global_memory.return_value = ""
    vault.get_user_profile.return_value = None
    vault.load_user_facts.return_value = []

    config = MagicMock()

    monkeypatch.setattr(sr, "llm_call_json", lambda **kw: _fake_llm_result())
    monkeypatch.setattr(
        "systemu.pipelines.activity_extractor.init_pipeline",
        lambda *a, **k: None, raising=False,
    )
    monkeypatch.setattr(
        "systemu.interface.notifications.set_vault",
        lambda *a, **k: None, raising=False,
    )

    # Intentional leading/trailing whitespace — raw_request must not strip it
    raw_prompt = "  Find me the top 10 best-selling SKUs\nfor March 2025  "
    scroll = sr.refine_from_text(raw_prompt, vault, config)

    assert scroll.raw_request == raw_prompt


def test_refine_from_text_scroll_status_is_approved(monkeypatch):
    """Sanity: chat scrolls are APPROVED immediately; raw_request is still set."""
    import systemu.pipelines.scroll_refiner as sr

    vault = MagicMock()
    vault.load_global_memory.return_value = ""
    vault.get_user_profile.return_value = None
    vault.load_user_facts.return_value = []

    config = MagicMock()

    monkeypatch.setattr(sr, "llm_call_json", lambda **kw: _fake_llm_result())
    monkeypatch.setattr(
        "systemu.pipelines.activity_extractor.init_pipeline",
        lambda *a, **k: None, raising=False,
    )
    monkeypatch.setattr(
        "systemu.interface.notifications.set_vault",
        lambda *a, **k: None, raising=False,
    )

    scroll = sr.refine_from_text("Any prompt", vault, config)

    assert scroll.status == ScrollStatus.APPROVED
    assert scroll.raw_request == "Any prompt"
