"""Tests for v0.6.0-b — Stage 6 intent-aware validator + remediation loop.

Covers:
  * Expanded catalog includes parameters_schema + return_schema (truncated)
  * ProposedRevision parsing from LLM output
  * New intent-aware blocker categories propagate end-to-end
  * Remediator accept_revision applies + re-validates + audits
  * Remediator override + workshop verbs write audit rows
  * Hard cap at MAX_REMEDIATION_CYCLES
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.pipelines import scroll_remediator as rm
from systemu.pipelines import scroll_validator as sv


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _config(supervisor_on=True):
    c = MagicMock()
    c.intelligent_supervisor_enabled = supervisor_on
    c.openrouter_api_key = "test"
    c.tier1_model = "gpt-test"
    return c


def _scroll_obj_ns(id_, goal, success_criteria, output_type=""):
    return SimpleNamespace(
        id=id_, goal=goal,
        success_criteria=success_criteria,
        output_type=output_type,
    )


def _scroll_ns(objectives=None, *, intent="document weather data", expected_outcome=""):
    return SimpleNamespace(
        id="scroll_test",
        name="t",
        intent=intent,
        expected_outcome=expected_outcome,
        objectives=objectives or [],
        constraints={},
    )


# ─────────────────────────────────────────────────────────────────────────────
# — expanded catalog: parameters_schema + return_schema

class TestCatalogExpansion:
    def test_catalog_includes_tool_schemas(self, vault):
        from systemu.core.models import Tool, ToolStatus, ToolType
        t = Tool(
            id="tool_x", name="fetch_json",
            description="Fetch JSON from a URL",
            tool_type=ToolType.PYTHON_FUNCTION,
            status=ToolStatus.DEPLOYED, enabled=True,
            parameters_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
            },
            return_schema={
                "type": "object",
                "properties": {"data": {"type": "object"}},
            },
        )
        vault.save_tool(t)

        catalog = sv._build_catalog(vault)
        assert len(catalog["tools"]) == 1
        entry = catalog["tools"][0]
        assert entry["name"] == "fetch_json"
        assert entry["parameters_schema"] == {"url": "string"}
        assert entry["return_schema"] == {"data": "object"}

    def test_catalog_handles_skills_without_intent_contract_fields(self, vault):
        # v0.6.0-d.5 fields (target_outcomes, produces) may not exist on
        # legacy / starter-pack skills — must default to empty lists.
        from systemu.core.models import Skill
        s = Skill(
            id="skill_legacy", name="legacy_skill",
            description="An old skill", category="general",
        )
        vault.save_skill(s)

        catalog = sv._build_catalog(vault)
        assert len(catalog["skills"]) == 1
        entry = catalog["skills"][0]
        assert entry["target_outcomes"] == []
        assert entry["produces"] == []


# ─────────────────────────────────────────────────────────────────────────────
# — ProposedRevision propagates through ValidationResult

class TestProposedRevisionParse:
    def test_revision_parsed_on_satisfiable_false(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll_ns([
            _scroll_obj_ns(1, "take screenshot", "image saved", output_type="image"),
        ])

        def fake_llm(**kw):
            return {
                "satisfiable": False,
                "confidence":  "high",
                "blockers": [{
                    "objective_id":  1,
                    "category":      "intent_mismatch",
                    "explanation":   "screenshot does not satisfy 'document weather data'",
                    "suggested_fix": "fetch JSON instead",
                }],
                "summary": "intent mismatch",
                "proposed_revision": {
                    "objectives": [
                        {
                            "id": 1,
                            "goal": "Fetch current weather JSON",
                            "success_criteria": "JSON with temp field received",
                            "output_type": "data",
                        },
                    ],
                    "rationale": "Replaces screenshot with API fetch to serve the data intent",
                },
            }
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)

        result = sv.validate_scroll(scroll, config=_config(), vault=vault)
        assert result.satisfiable is False
        assert result.proposed_revision is not None
        assert len(result.proposed_revision.objectives) == 1
        assert result.proposed_revision.objectives[0]["goal"] == "Fetch current weather JSON"
        assert "API fetch" in result.proposed_revision.rationale

    def test_revision_absent_on_satisfiable_true(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll_ns([
            _scroll_obj_ns(1, "g", "sc"),
        ])

        def fake_llm(**kw):
            return {
                "satisfiable": True,
                "confidence":  "high",
                "blockers":    [],
                "summary":     "ok",
                # Even if LLM erroneously emits a revision on a pass, parser
                # should ignore it.
                "proposed_revision": {"objectives": [{"id": 1, "goal": "x"}]},
            }
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)

        result = sv.validate_scroll(scroll, config=_config(), vault=vault)
        assert result.satisfiable is True
        assert result.proposed_revision is None

    def test_malformed_revision_silently_dropped(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll_ns([_scroll_obj_ns(1, "g", "sc")])

        def fake_llm(**kw):
            return {
                "satisfiable": False,
                "confidence":  "medium",
                "blockers":    [],
                "summary":     "x",
                "proposed_revision": "not a dict",
            }
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)

        result = sv.validate_scroll(scroll, config=_config(), vault=vault)
        assert result.satisfiable is False
        assert result.proposed_revision is None    # malformed → None, not crash


# ─────────────────────────────────────────────────────────────────────────────
# — new intent-aware blocker categories

class TestNewBlockerCategories:
    def test_intent_mismatch_category_parses(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll_ns([_scroll_obj_ns(1, "g", "sc")])

        for cat in ("intent_mismatch", "data_flow_break",
                    "output_type_mismatch", "outcome_mismatch"):
            def fake_llm(*, _cat=cat, **kw):
                return {
                    "satisfiable": False, "confidence": "high",
                    "blockers": [{
                        "objective_id": 1, "category": _cat,
                        "explanation": "x", "suggested_fix": "y",
                    }],
                    "summary": "",
                }
            monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)
            result = sv.validate_scroll(scroll, config=_config(), vault=vault)
            assert result.blockers[0].category == cat


# ─────────────────────────────────────────────────────────────────────────────
# — Remediator: accept_revision applies + re-validates

class TestAcceptRevision:
    def test_accept_replaces_objectives_and_revalidates(self, vault, tmp_path, monkeypatch):
        from systemu.core.models import Objective, Scroll

        scroll = Scroll(
            id="scroll_rev1", name="t",
            source_session_id="s", raw_instructions_path="",
            narrative_md="", intent="document weather data",
            objectives=[
                Objective(id=1, goal="take screenshot",
                          success_criteria="image saved", output_type="image"),
            ],
        )
        vault.save_scroll(scroll)

        proposed = sv.ProposedRevision(
            objectives=[{
                "id": 1, "goal": "Fetch JSON",
                "success_criteria": "JSON received",
                "output_type": "data",
            }],
            rationale="serves data intent",
        )

        # Re-validation returns satisfiable now
        call_count = [0]

        def fake_llm(**kw):
            call_count[0] += 1
            return {
                "satisfiable": True, "confidence": "high",
                "blockers": [], "summary": "fixed",
            }

        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)

        result = rm.accept_revision(
            scroll, proposed,
            config=_config(), vault=vault,
            cycle=1, operator_note="auto-test",
            data_dir=tmp_path,
        )

        # Scroll objectives were replaced
        reloaded = vault.get_scroll(scroll.id)
        assert reloaded.objectives[0].goal == "Fetch JSON"
        assert reloaded.objectives[0].output_type == "data"

        # Result is the re-validation outcome
        assert result.satisfiable is True

        # Audit record written
        audit = (tmp_path / "scroll_remediations.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in audit.strip().splitlines()]
        assert len(rows) >= 1
        accept = [r for r in rows if r["action"] == "accept"][-1]
        assert accept["cycle"] == 1
        assert accept["result_after"] == "satisfiable"

    def test_accept_at_cap_does_not_revalidate(self, vault, tmp_path, monkeypatch):
        from systemu.core.models import Objective, Scroll

        scroll = Scroll(
            id="scroll_cap", name="t", source_session_id="s",
            raw_instructions_path="", narrative_md="",
            intent="x",
            objectives=[Objective(id=1, goal="g", success_criteria="sc")],
        )
        vault.save_scroll(scroll)

        proposed = sv.ProposedRevision(
            objectives=[{"id": 1, "goal": "rev", "success_criteria": "rsc"}],
        )

        # LLM should not be called when capped — but make sure it WOULD blow up
        # if it were, so the test fails loud.
        def boom(**kw):
            raise AssertionError("validator must not be called once cap is hit")
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", boom)

        result = rm.accept_revision(
            scroll, proposed,
            config=_config(), vault=vault,
            cycle=rm.MAX_REMEDIATION_CYCLES + 1,
            data_dir=tmp_path,
        )

        assert result.satisfiable is False
        assert "cap reached" in result.summary.lower()


# ─────────────────────────────────────────────────────────────────────────────
# — Remediator: override + workshop audit

class TestOverrideAndWorkshop:
    def test_override_writes_audit_row(self, vault, tmp_path):
        from systemu.core.models import Scroll
        scroll = Scroll(
            id="scroll_ovr", name="t", source_session_id="s",
            raw_instructions_path="", narrative_md="", intent="x",
        )
        rm.override_revision(
            scroll,
            blockers=[{"category": "intent_mismatch", "explanation": "x"}],
            operator_note="I know what I'm doing",
            data_dir=tmp_path,
        )
        rows = [
            json.loads(line)
            for line in (tmp_path / "scroll_remediations.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert any(r["action"] == "override" for r in rows)

    def test_workshop_writes_audit_row(self, vault, tmp_path):
        from systemu.core.models import Scroll
        scroll = Scroll(
            id="scroll_wk", name="t", source_session_id="s",
            raw_instructions_path="", narrative_md="", intent="x",
        )
        rm.route_to_workshop(scroll, blockers=[], data_dir=tmp_path)
        rows = [
            json.loads(line)
            for line in (tmp_path / "scroll_remediations.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert any(r["action"] == "workshop" for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# — publish_remediation_card no-ops without revision

class TestPublishCard:
    def test_no_op_when_no_revision(self, vault):
        from systemu.core.models import Scroll
        scroll = Scroll(
            id="scroll_no_rev", name="t", source_session_id="s",
            raw_instructions_path="", narrative_md="", intent="x",
        )
        result = sv.ValidationResult(
            satisfiable=False, confidence="medium",
            blockers=[], summary="x",
            proposed_revision=None,
        )
        # Should not raise.  No-op exit is the assertion (we'd otherwise need to
        # mock EventBus, which is overkill for a no-op test).
        rm.publish_remediation_card(scroll, result)
