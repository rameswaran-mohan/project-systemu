"""Tests for Vault CRUD operations."""
import json
import pytest
import tempfile
from pathlib import Path

from systemu.core.models import (
    Scroll, ScrollStatus, Objective,
    Tool, ToolStatus, ToolType,
    Skill, Activity, ActivityStatus, Shadow,
)
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault


@pytest.fixture
def tmp_vault(tmp_path):
    """Create a fresh Vault rooted in a temp directory."""
    # Create required subdirectories
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    # Seed empty indexes
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "notifications" / "pending.json").write_text("[]", encoding="utf-8")
    (tmp_path / "notifications" / "event_log.jsonl").write_text("", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def sample_scroll():
    return Scroll(
        id=generate_id("scroll"),
        name="Test Scroll",
        source_session_id="sess_abc",
        raw_instructions_path="/tmp/instructions.md",
        narrative_md="Test narrative.",
        intent="Accomplish the test task.",
        objectives=[
            Objective(id=1, goal="Do step one", success_criteria="Step one done"),
            Objective(id=2, goal="Do step two", success_criteria="Step two done", depends_on=[1]),
        ],
    )


@pytest.fixture
def sample_tool():
    return Tool(
        id=generate_id("tool"),
        name="web_screenshot",
        description="Screenshot a URL",
        tool_type=ToolType.BROWSER_ACTION,
        status=ToolStatus.DEPLOYED,
        enabled=True,
        implementation_path="vault/tools/implementations/web_screenshot.py",
    )


@pytest.fixture
def sample_skill():
    return Skill(
        id=generate_id("skill"),
        name="web_data_capture",
        description="Capture web data programmatically",
        category="browser",
        proficiency_level="intermediate",
        required_tool_names=["web_screenshot"],
        instructions_md="Use web_screenshot to capture URLs.",
    )


# ─── Scroll CRUD ──────────────────────────────────────────────────────────────

class TestScrollVault:
    def test_save_and_get(self, tmp_vault, sample_scroll):
        tmp_vault.save_scroll(sample_scroll)
        loaded = tmp_vault.get_scroll(sample_scroll.id)
        assert loaded.id == sample_scroll.id
        assert loaded.name == sample_scroll.name
        assert loaded.intent == sample_scroll.intent

    def test_objectives_round_trip(self, tmp_vault, sample_scroll):
        tmp_vault.save_scroll(sample_scroll)
        loaded = tmp_vault.get_scroll(sample_scroll.id)
        assert len(loaded.objectives) == 2
        assert loaded.objectives[0].goal == "Do step one"
        assert loaded.objectives[1].depends_on == [1]

    def test_index_updated_on_save(self, tmp_vault, sample_scroll):
        tmp_vault.save_scroll(sample_scroll)
        index = tmp_vault.load_index("scrolls")
        ids = [e["id"] for e in index]
        assert sample_scroll.id in ids

    def test_get_nonexistent_raises(self, tmp_vault):
        with pytest.raises(KeyError):
            tmp_vault.get_scroll("scroll_doesnotexist")

    def test_status_update_persists(self, tmp_vault, sample_scroll):
        tmp_vault.save_scroll(sample_scroll)
        sample_scroll.status = ScrollStatus.APPROVED
        tmp_vault.save_scroll(sample_scroll)
        loaded = tmp_vault.get_scroll(sample_scroll.id)
        assert loaded.status == ScrollStatus.APPROVED

    def test_multiple_scrolls_in_index(self, tmp_vault):
        scrolls = [
            Scroll(
                id=generate_id("scroll"), name=f"Scroll {i}",
                source_session_id="sess", raw_instructions_path="/tmp/x.md",
                narrative_md="n",
            )
            for i in range(3)
        ]
        for s in scrolls:
            tmp_vault.save_scroll(s)
        index = tmp_vault.load_index("scrolls")
        assert len(index) == 3


# ─── Tool CRUD ────────────────────────────────────────────────────────────────

class TestToolVault:
    def test_save_and_get(self, tmp_vault, sample_tool):
        tmp_vault.save_tool(sample_tool)
        loaded = tmp_vault.get_tool(sample_tool.id)
        assert loaded.name == sample_tool.name
        assert loaded.status == ToolStatus.DEPLOYED
        assert loaded.enabled is True

    def test_index_entry(self, tmp_vault, sample_tool):
        tmp_vault.save_tool(sample_tool)
        index = tmp_vault.load_index("tools")
        names = [t["name"] for t in index]
        assert sample_tool.name in names

    def test_get_nonexistent_raises(self, tmp_vault):
        with pytest.raises(KeyError):
            tmp_vault.get_tool("tool_doesnotexist")

    def test_update_tool_status(self, tmp_vault, sample_tool):
        tmp_vault.save_tool(sample_tool)
        sample_tool.status = ToolStatus.UPGRADED
        tmp_vault.save_tool(sample_tool)
        loaded = tmp_vault.get_tool(sample_tool.id)
        assert loaded.status == ToolStatus.UPGRADED


# ─── Skill CRUD ───────────────────────────────────────────────────────────────

class TestSkillVault:
    def test_save_and_get(self, tmp_vault, sample_skill):
        tmp_vault.save_skill(sample_skill)
        loaded = tmp_vault.get_skill(sample_skill.id)
        assert loaded.name == sample_skill.name
        assert loaded.category == "browser"

    def test_skill_md_written(self, tmp_vault, sample_skill):
        tmp_vault.save_skill(sample_skill)
        md_path = Path(sample_skill.skill_md_path)
        assert md_path.exists()
        content = md_path.read_text()
        # v0.7.1: vault.save_skill kebab-cases the on-disk name. The internal
        # Skill.name (e.g., "web_data_capture") is preserved in the Python
        # object; the SKILL.md uses the spec-conformant kebab form.
        kebab_name = sample_skill.name.replace("_", "-").lower()
        assert kebab_name in content

    def test_index_entry(self, tmp_vault, sample_skill):
        tmp_vault.save_skill(sample_skill)
        index = tmp_vault.load_index("skills")
        assert any(e["id"] == sample_skill.id for e in index)


# ─── Index deduplication ──────────────────────────────────────────────────────

class TestIndexDeduplication:
    def test_same_scroll_saved_twice_no_duplicate(self, tmp_vault, sample_scroll):
        tmp_vault.save_scroll(sample_scroll)
        tmp_vault.save_scroll(sample_scroll)
        index = tmp_vault.load_index("scrolls")
        matching = [e for e in index if e["id"] == sample_scroll.id]
        assert len(matching) == 1

    def test_same_tool_saved_twice_no_duplicate(self, tmp_vault, sample_tool):
        tmp_vault.save_tool(sample_tool)
        tmp_vault.save_tool(sample_tool)
        index = tmp_vault.load_index("tools")
        matching = [e for e in index if e["id"] == sample_tool.id]
        assert len(matching) == 1
