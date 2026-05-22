"""Tests for the v0.3.4 forge-prompt nudge.

The Tool Forge spec step now passes the operator-approved pip allow-list
to the spec LLM as ``preferred_packages``, so the LLM prefers already-
approved deps when there's a real choice.  Novel deps still go through
the runtime's PROMPT gate; the nudge is advisory only.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from systemu.core.models import ToolStatus, ToolType
from systemu.runtime.dep_approvals import DepApprovalStore


@pytest.fixture
def tmp_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def cfg(tmp_path):
    c = MagicMock()
    c.vault_dir = str(tmp_path)
    c.tier2_model = "test"
    return c


def _seed_approvals(monkeypatch, tmp_path: Path, packages: list[str]) -> None:
    """Point the forge at a fresh approval store living under ``tmp_path``
    so different tests don't bleed approvals into each other."""
    store_dir = tmp_path / "data"
    store_dir.mkdir(exist_ok=True)
    store = DepApprovalStore(store_dir / "dep_approvals.json")
    for p in packages:
        store.approve(p)
    # Make _approved_packages_hint() pick up THIS store, not the global
    # data/ dir, by monkeypatching the CWD-relative path used inside the
    # helper.  Cleanest seam: monkeypatch the helper to read from our
    # temp directory.
    from systemu.pipelines import tool_forge

    def _hint():
        return [e["package"] for e in store.list_approved()]

    monkeypatch.setattr(tool_forge, "_approved_packages_hint", _hint)


def test_forge_payload_includes_preferred_packages(monkeypatch, tmp_path, tmp_vault, cfg):
    _seed_approvals(monkeypatch, tmp_path, ["python-docx", "requests"])

    captured = {}

    def fake_llm_call_json(*, tier, system, user, config, temperature, max_tokens):
        # Capture the spec-step call (first call); skip subsequent code step.
        if "spec" not in captured:
            captured["spec"] = json.loads(user)
            return {
                "name":                "new_word_tool",
                "description":         "test",
                "tool_type":           "python_function",
                "parameters_schema":   {},
                "return_schema":       {"success": {"type": "boolean"}},
                "implementation_notes": "test",
                "dependencies":        ["python-docx"],
            }
        # Code step — return minimal valid result.
        return {"implementation": "def run():\n    return {'success': True}"}

    monkeypatch.setattr(
        "systemu.pipelines.tool_forge.llm_call_json", fake_llm_call_json,
    )
    monkeypatch.setattr(
        "systemu.pipelines.tool_forge.notify_user", lambda *a, **kw: "Forge",
    )

    from systemu.pipelines.tool_forge import _spec_and_forge_new
    result = _spec_and_forge_new(
        "new_word_tool",
        context_hint="capture and document weather",
        config=cfg,
        vault=tmp_vault,
    )
    assert result is not None
    assert "preferred_packages" in captured["spec"]
    assert sorted(captured["spec"]["preferred_packages"]) == ["python-docx", "requests"]


def test_forge_payload_with_no_approvals_lists_empty(monkeypatch, tmp_path, tmp_vault, cfg):
    _seed_approvals(monkeypatch, tmp_path, [])  # empty allow-list

    captured = {}

    def fake_llm_call_json(*, tier, system, user, config, temperature, max_tokens):
        if "spec" not in captured:
            captured["spec"] = json.loads(user)
            return {
                "name":                "blank_tool",
                "description":         "test",
                "tool_type":           "python_function",
                "parameters_schema":   {},
                "return_schema":       {"success": {"type": "boolean"}},
                "implementation_notes": "test",
                "dependencies":        [],
            }
        return {"implementation": "def run():\n    return {'success': True}"}

    monkeypatch.setattr(
        "systemu.pipelines.tool_forge.llm_call_json", fake_llm_call_json,
    )
    monkeypatch.setattr(
        "systemu.pipelines.tool_forge.notify_user", lambda *a, **kw: "Forge",
    )

    from systemu.pipelines.tool_forge import _spec_and_forge_new
    result = _spec_and_forge_new(
        "blank_tool", context_hint="hint", config=cfg, vault=tmp_vault,
    )
    assert result is not None
    assert captured["spec"]["preferred_packages"] == []
