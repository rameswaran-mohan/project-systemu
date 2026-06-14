"""Plan 0 Build 3 (Task 3.3) — subagent_harness child builders.

  * build_child_shadow(parent_shadow, child_id)
      child inherits parent's tool/skill ids MINUS any delegate/spawn_subagent
      tool (hard non-recursion), has a distinct id and the parent's
      system_prompt.
  * build_child_activity(parent_activity, subtask, child_id, vault)
      saves a child Scroll (single Objective whose goal is the subtask) and a
      child Activity referencing it; both retrievable from the vault.
"""
import pytest

from systemu.core.models import (
    Activity,
    ActivityStatus,
    Shadow,
    ShadowStatus,
)
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault
from systemu.runtime.subagent_harness import (
    build_child_shadow,
    build_child_activity,
)


@pytest.fixture
def tmp_vault(tmp_path):
    return Vault(str(tmp_path))


@pytest.fixture
def parent_shadow():
    return Shadow(
        id=generate_id("shadow"),
        name="Parent Shadow",
        description="A parent shadow with delegate access.",
        identity_block="You are a careful, parent orchestrator agent.",
        available_tool_ids=["tool_read", "delegate", "spawn_subagent", "tool_write"],
        skill_ids=["skill_research", "skill_summarise"],
        status=ShadowStatus.ACTIVE,
    )


@pytest.fixture
def parent_activity():
    return Activity(
        id=generate_id("activity"),
        name="Parent Activity",
        scroll_id=generate_id("scroll"),
        required_tool_ids=["tool_read"],
        required_skill_ids=["skill_research"],
        assigned_shadow_id=generate_id("shadow"),
        status=ActivityStatus.ASSIGNED,
    )


class TestBuildChildShadow:
    def test_excludes_delegate_tools(self, parent_shadow):
        child = build_child_shadow(parent_shadow, "child_1")
        assert "delegate" not in child.available_tool_ids
        assert "spawn_subagent" not in child.available_tool_ids
        # Non-delegate tools survive.
        assert "tool_read" in child.available_tool_ids
        assert "tool_write" in child.available_tool_ids

    def test_distinct_id(self, parent_shadow):
        child = build_child_shadow(parent_shadow, "child_1")
        assert child.id != parent_shadow.id

    def test_inherits_skills_and_system_prompt(self, parent_shadow):
        child = build_child_shadow(parent_shadow, "child_1")
        assert child.skill_ids == parent_shadow.skill_ids
        # system_prompt is a computed field derived from identity_block; the
        # child must carry the parent's prompt verbatim.
        assert child.system_prompt == parent_shadow.system_prompt


class TestBuildChildActivity:
    def test_creates_retrievable_scroll_and_activity(
        self, parent_activity, tmp_vault
    ):
        subtask = "Fetch and summarise the quarterly numbers"
        child_activity = build_child_activity(
            parent_activity, subtask, "child_1", tmp_vault
        )

        # Activity is retrievable.
        loaded_activity = tmp_vault.get_activity(child_activity.id)
        assert loaded_activity.id == child_activity.id

        # Its scroll is retrievable and has exactly one objective whose goal
        # is the subtask.
        scroll = tmp_vault.get_scroll(loaded_activity.scroll_id)
        assert len(scroll.objectives) == 1
        assert scroll.objectives[0].goal == subtask

    def test_activity_references_child_scroll(self, parent_activity, tmp_vault):
        child_activity = build_child_activity(
            parent_activity, "do the thing", "child_2", tmp_vault
        )
        assert child_activity.scroll_id
        # The scroll id on the activity matches a real saved scroll.
        scroll = tmp_vault.get_scroll(child_activity.scroll_id)
        assert scroll.id == child_activity.scroll_id

    def test_child_activity_distinct_from_parent(self, parent_activity, tmp_vault):
        child_activity = build_child_activity(
            parent_activity, "sub", "child_3", tmp_vault
        )
        assert child_activity.id != parent_activity.id
        assert child_activity.scroll_id != parent_activity.scroll_id
