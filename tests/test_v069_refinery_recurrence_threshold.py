"""refinery only writes a 'failure_patterns' lesson after N>=3
distinct executions corroborate the same signature.

Single-incident "failure_patterns" lessons were causing memory poisoning:
one failed run would write "tool has persistent failures" to the buffer,
the next run would read that lesson and refuse to retry the tool —
even after the operator had fixed the underlying cause.

Observational categories (tool_quirks, heuristics, domain_glossary,
self_assessment) are not gated — they describe stable facts about a
tool or domain rather than asserting a resolvable failure.
"""
from __future__ import annotations
from unittest.mock import MagicMock


def _fake_lesson(category: str = "failure_patterns",
                 tool_name: str = "fetch_json",
                 text: str = "tool fails persistently") -> dict:
    return {
        "category": category,
        "tool_name": tool_name,
        "lesson": text,
        "keyword": "fail",
        "evidence_action_blocks": [1],
    }


def _patch_refinery_dependencies(monkeypatch, lessons, prior_entries=None):
    """Patch refinery's LLM + vault-load helpers.

    The refinery uses ``vault.load_shadow_memory(shadow_id)`` (via the
    private ``_existing_signatures`` helper) to read the buffer.  We patch
    that directly via the vault mock — both signature dedup and the new
    N>=3 occurrence count read from the same source of truth.
    """
    from systemu.pipelines import refinery
    monkeypatch.setattr(refinery, "llm_call_json",
                        lambda **kw: {"lessons": lessons})
    return refinery


def _make_vault(prior_entries=None):
    vault = MagicMock()
    # load_shadow_memory returns (md_text, list_of_entries)
    vault.load_shadow_memory.return_value = ("", list(prior_entries or []))
    return vault


def test_single_occurrence_failure_pattern_is_skipped(monkeypatch):
    refinery = _patch_refinery_dependencies(monkeypatch, [_fake_lesson()])
    vault = _make_vault(prior_entries=[])
    shadow = MagicMock(id="sh1", name="x", description="")
    scroll = MagicMock(name="s", action_blocks=[], objectives=[])
    refinery._extract_memory_candidates(
        shadow, scroll, {"status": "failure", "execution_id": "e1"},
        history_json=[], config=MagicMock(), vault=vault,
    )
    vault.append_shadow_memory_buffer.assert_not_called()


def test_failure_pattern_writes_only_on_third_occurrence(monkeypatch):
    refinery = _patch_refinery_dependencies(monkeypatch, [_fake_lesson()])
    from systemu.core.memory_types import pattern_signature
    sig = pattern_signature(
        error_type="failure_patterns",
        tool_name="fetch_json",
        error_message="tool fails persistently",
        top_keyword="fail",
    )
    vault = _make_vault(prior_entries=[
        {"_pattern_signature": sig, "exec_id": "e_prev1"},
        {"_pattern_signature": sig, "exec_id": "e_prev2"},
    ])
    shadow = MagicMock(id="sh1", name="x", description="")
    scroll = MagicMock(name="s", action_blocks=[], objectives=[])
    refinery._extract_memory_candidates(
        shadow, scroll, {"status": "failure", "execution_id": "e_new"},
        history_json=[], config=MagicMock(), vault=vault,
    )
    vault.append_shadow_memory_buffer.assert_called_once()


def test_non_failure_categories_write_unconditionally(monkeypatch):
    lesson = _fake_lesson(category="tool_quirks", text="returns 200 on empty body")
    refinery = _patch_refinery_dependencies(monkeypatch, [lesson])
    vault = _make_vault(prior_entries=[])
    shadow = MagicMock(id="sh1", name="x", description="")
    scroll = MagicMock(name="s", action_blocks=[], objectives=[])
    refinery._extract_memory_candidates(
        shadow, scroll, {"status": "success", "execution_id": "e1"},
        history_json=[], config=MagicMock(), vault=vault,
    )
    vault.append_shadow_memory_buffer.assert_called_once()
