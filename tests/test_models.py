"""Tests for Pydantic models — Objective, Scroll, Tool, Skill, Shadow, Activity."""
import pytest
from datetime import datetime
from systemu.core.models import (
    ActionBlock, Objective, Scroll, ScrollStatus,
    Tool, ToolStatus, ToolType,
    Skill, Activity, ActivityStatus,
    Shadow, ShadowStatus,
    Objective,
)


# ─── Objective ────────────────────────────────────────────────────────────────

class TestObjective:
    def test_basic_creation(self):
        obj = Objective(id=1, goal="Do something", success_criteria="It got done")
        assert obj.id == 1
        assert obj.goal == "Do something"
        assert obj.success_criteria == "It got done"
        assert obj.output_type == ""
        assert obj.hints == {}
        assert obj.depends_on == []

    def test_full_creation(self):
        obj = Objective(
            id=2,
            goal="Download report",
            success_criteria="report.pdf exists",
            output_type="file",
            hints={"output_path": "~/Documents/", "format": "pdf"},
            depends_on=[1],
        )
        assert obj.output_type == "file"
        assert obj.hints["output_path"] == "~/Documents/"
        assert obj.depends_on == [1]

    def test_model_dump_json(self):
        obj = Objective(id=1, goal="test", success_criteria="done")
        d = obj.model_dump(mode="json")
        assert isinstance(d, dict)
        assert d["id"] == 1
        assert d["goal"] == "test"

    def test_model_validate_from_dict(self):
        raw = {"id": 3, "goal": "fetch data", "success_criteria": "data returned", "output_type": "data"}
        obj = Objective.model_validate(raw)
        assert obj.id == 3
        assert obj.output_type == "data"


# ─── Scroll ───────────────────────────────────────────────────────────────────

class TestScroll:
    def _make_scroll(self, **kwargs):
        defaults = dict(
            id="scroll_abc123",
            name="Test Scroll",
            source_session_id="sess_001",
            raw_instructions_path="/tmp/instructions.md",
            narrative_md="Test narrative.",
        )
        defaults.update(kwargs)
        return Scroll(**defaults)

    def test_intent_driven_scroll(self):
        objs = [
            Objective(id=1, goal="Fetch data", success_criteria="data fetched"),
            Objective(id=2, goal="Write report", success_criteria="report.docx exists", depends_on=[1]),
        ]
        scroll = self._make_scroll(
            intent="Fetch financial data and create a report.",
            objectives=objs,
            constraints={"output_location": "~/Documents/"},
            observed_preferences={"date_format": "MMDDYYYY"},
        )
        assert scroll.intent == "Fetch financial data and create a report."
        assert len(scroll.objectives) == 2
        assert scroll.objectives[1].depends_on == [1]
        assert scroll.constraints["output_location"] == "~/Documents/"
        assert scroll.action_blocks == []  # empty for intent-driven

    def test_legacy_scroll_with_action_blocks(self):
        ab = ActionBlock(step_number=1, action="navigate", target="https://example.com")
        scroll = self._make_scroll(action_blocks=[ab])
        assert len(scroll.action_blocks) == 1
        assert scroll.objectives == []  # legacy has no objectives

    def test_default_status(self):
        scroll = self._make_scroll()
        assert scroll.status == ScrollStatus.DRAFT

    def test_backward_compat_empty_intent(self):
        scroll = self._make_scroll()
        assert scroll.intent == ""
        assert scroll.constraints == {}
        assert scroll.observed_preferences == {}

    def test_model_dump_includes_all_fields(self):
        scroll = self._make_scroll(intent="test intent")
        d = scroll.model_dump(mode="json")
        assert "intent" in d
        assert "objectives" in d
        assert "constraints" in d
        assert "observed_preferences" in d
        assert "action_blocks" in d


# ─── Tool ─────────────────────────────────────────────────────────────────────

class TestTool:
    def test_tool_defaults(self):
        t = Tool(
            id="tool_001", name="web_screenshot", description="Capture a screenshot",
            tool_type=ToolType.BROWSER_ACTION,
        )
        assert t.status == ToolStatus.PROPOSED
        assert t.enabled is False
        assert t.dependencies == []

    def test_tool_type_enum(self):
        assert ToolType("browser_action") == ToolType.BROWSER_ACTION
        assert ToolType("file_operation") == ToolType.FILE_OPERATION

    def test_deployed_enabled_tool(self):
        t = Tool(
            id="tool_002", name="file_read", description="Read a file",
            tool_type=ToolType.FILE_OPERATION,
            status=ToolStatus.DEPLOYED,
            enabled=True,
            implementation_path="vault/tools/implementations/file_read.py",
        )
        assert t.status == ToolStatus.DEPLOYED
        assert t.enabled is True


# ─── Skill ────────────────────────────────────────────────────────────────────

class TestSkill:
    def test_skill_creation(self):
        s = Skill(
            id="skill_001", name="web_data_capture",
            description="Capture web data",
            category="browser",
            proficiency_level="intermediate",
            required_tool_names=["web_screenshot", "web_extract_text"],
            instructions_md="To capture web data: 1) Use web_screenshot...",
        )
        assert s.category == "browser"
        assert len(s.required_tool_names) == 2
        assert s.evidence_scroll_ids == []

    def test_skill_evidence_update(self):
        s = Skill(
            id="skill_002", name="file_management",
            description="Manage files",
            category="file_ops",
            evidence_scroll_ids=["scroll_abc"],
        )
        s.evidence_scroll_ids.append("scroll_def")
        assert len(s.evidence_scroll_ids) == 2


# ─── Activity ─────────────────────────────────────────────────────────────────

class TestActivity:
    def test_default_status_unassigned(self):
        a = Activity(id="act_001", name="Test", scroll_id="scroll_001")
        assert a.status == ActivityStatus.UNASSIGNED
        assert a.required_tool_ids == []

    def test_partial_when_missing_tools(self):
        a = Activity(
            id="act_002", name="Test",
            scroll_id="scroll_001",
            required_tool_ids=["tool_a", "tool_b"],
            missing_tools=["tool_b"],
            status=ActivityStatus.PARTIAL,
        )
        assert a.status == ActivityStatus.PARTIAL
        assert "tool_b" in a.missing_tools


# ─── Shadow ───────────────────────────────────────────────────────────────────

class TestShadow:
    def test_default_status_dormant(self):
        s = Shadow(
            id="shadow_001", name="TestShadow",
            description="A test shadow", system_prompt="You are a test agent.",
        )
        assert s.status == ShadowStatus.DORMANT
        assert s.execution_log == []

    def test_execution_log_append(self):
        s = Shadow(
            id="shadow_001", name="TestShadow",
            description="A test shadow", system_prompt="...",
        )
        s.execution_log.append({"execution_id": "exec_001", "status": "success"})
        assert len(s.execution_log) == 1


# ─── ActionBlock (legacy) ─────────────────────────────────────────────────────

class TestActionBlock:
    def test_action_block_creation(self):
        ab = ActionBlock(
            step_number=1, action="navigate",
            target="https://example.com",
            expected_outcome="Page loads",
            application="Chrome",
        )
        assert ab.step_number == 1
        assert ab.action == "navigate"
        assert ab.parameters == {}

    def test_model_dump(self):
        ab = ActionBlock(step_number=2, action="click", target="button#submit")
        d = ab.model_dump(mode="json")
        assert d["step_number"] == 2
        assert d["action"] == "click"
