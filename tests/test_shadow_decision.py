import json
import pytest
from unittest.mock import patch, MagicMock

from systemu.core.models import Activity, ActivityStatus, Scroll, ScrollStatus, Shadow, ShadowStatus, Tool, Skill
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))

@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.vault_dir = str(tmp_path)
    return cfg

def test_shadow_decision_partial_chat_activity_wildcard(tmp_vault, mock_config):
    # Chat-originated PARTIAL activity → Wild Card assigned immediately
    scroll = Scroll(
        id="scroll_chat_1",
        name="Chat Scroll",
        source_session_id="chat",
        raw_instructions_path="",
        narrative_md="Test",
    )
    tmp_vault.save_scroll(scroll)

    activity = Activity(
        id=generate_id("act"),
        name="Test",
        scroll_id="scroll_chat_1",
        status=ActivityStatus.PARTIAL,
    )
    tmp_vault.save_activity(activity)

    from systemu.pipelines.shadow_decision import decide_shadow
    shadow = decide_shadow(activity, mock_config, tmp_vault)

    assert shadow is not None
    assert shadow.name == "Wild Card"
    assert activity.status == ActivityStatus.ASSIGNED
    assert activity.assigned_shadow_id == shadow.id


def test_shadow_decision_partial_capture_activity_deferred(tmp_vault, mock_config):
    # Capture-based PARTIAL activity → deferred until tools are deployed
    scroll = Scroll(
        id="scroll_cap_1",
        name="Capture Scroll",
        source_session_id="session_abc123",
        raw_instructions_path="",
        narrative_md="Test",
    )
    tmp_vault.save_scroll(scroll)

    activity = Activity(
        id=generate_id("act"),
        name="Test Capture",
        scroll_id="scroll_cap_1",
        status=ActivityStatus.PARTIAL,
    )
    tmp_vault.save_activity(activity)

    from systemu.pipelines.shadow_decision import decide_shadow
    shadow = decide_shadow(activity, mock_config, tmp_vault)

    assert shadow is None
    assert activity.status == ActivityStatus.PARTIAL

def test_shadow_decision_exact_match(tmp_vault, mock_config):
    # Perfect match shadow (score 1.0)
    shadow = Shadow(
        id="shadow_perfect",
        name="Specialist",
        description="Perfect match",
        system_prompt="You are a perfect match.",
        skill_ids=["skill_1"],
        available_tool_ids=["tool_1"],
        assigned_activity_ids=[],
        status=ShadowStatus.AWAKENED
    )
    tmp_vault.save_shadow(shadow)

    activity = Activity(
        id=generate_id("act"),
        name="Test",
        scroll_id="scroll_1",
        required_skill_ids=["skill_1"],
        required_tool_ids=["tool_1"],
        status=ActivityStatus.UNASSIGNED
    )
    tmp_vault.save_activity(activity)

    from systemu.pipelines.shadow_decision import decide_shadow
    assigned_shadow = decide_shadow(activity, mock_config, tmp_vault)

    assert assigned_shadow is not None
    assert assigned_shadow.id == shadow.id
    assert activity.status == ActivityStatus.ASSIGNED
    assert activity.assigned_shadow_id == shadow.id

def test_shadow_decision_llm_tiebreak_assign_existing(tmp_vault, mock_config):
    # Partial match
    shadow = Shadow(
        id="shadow_partial",
        name="Partial Specialist",
        description="Partial match",
        system_prompt="You are a partial match.",
        skill_ids=["skill_1"],
        available_tool_ids=["tool_something_else"],
        assigned_activity_ids=[],
        status=ShadowStatus.AWAKENED
    )
    tmp_vault.save_shadow(shadow)

    activity = Activity(
        id=generate_id("act"),
        name="Test",
        scroll_id="scroll_1",
        required_skill_ids=["skill_1"],
        required_tool_ids=["tool_1"], # mismatch
        status=ActivityStatus.UNASSIGNED
    )
    tmp_vault.save_activity(activity)

    mock_llm_response = {
        "decision": "ASSIGN_EXISTING",
        "target_shadow_id": "shadow_partial",
        "new_skills_to_tag": [],
        "new_tools_to_tag": ["tool_1"]
    }

    with patch("systemu.pipelines.shadow_decision.llm_call_json", return_value=mock_llm_response):
        with patch("systemu.pipelines.shadow_decision.notify_user"):
            from systemu.pipelines.shadow_decision import decide_shadow
            assigned_shadow = decide_shadow(activity, mock_config, tmp_vault)

            assert assigned_shadow is not None
            assert assigned_shadow.id == shadow.id
            assert "tool_1" in assigned_shadow.available_tool_ids

def test_shadow_decision_llm_create_new(tmp_vault, mock_config):
    # Add a partial match to trigger LLM tiebreak (score >= 0.4)
    shadow_partial = Shadow(
        id="shadow_partial",
        name="Partial Specialist",
        description="Partial match",
        system_prompt="You are a partial match.",
        skill_ids=["skill_1"],
        available_tool_ids=["tool_something_else"],
        assigned_activity_ids=[],
        status=ShadowStatus.AWAKENED
    )
    tmp_vault.save_shadow(shadow_partial)

    activity = Activity(
        id=generate_id("act"),
        name="Test",
        scroll_id="scroll_1",
        required_skill_ids=["skill_1"],
        required_tool_ids=["tool_1"],
        status=ActivityStatus.UNASSIGNED
    )
    tmp_vault.save_activity(activity)

    mock_llm_tiebreak = {
        "decision": "CREATE_NEW",
        "proposed_shadow_name_hint": "NewShadow",
        "reasoning": "Needs new persona"
    }

    mock_persona = {
        "description": "A totally new persona.",
        "system_prompt": "You are new."
    }

    # Flow: tiebreak -> _prompt_create_new -> notify_user("Awaken") -> create_shadow -> llm_call_json(persona)
    with patch("systemu.pipelines.shadow_decision.llm_call_json", side_effect=[mock_llm_tiebreak, mock_persona]):
        with patch("systemu.pipelines.shadow_decision.notify_user", return_value="Awaken: Test Persona"):
            from systemu.pipelines.shadow_decision import decide_shadow
            assigned_shadow = decide_shadow(activity, mock_config, tmp_vault)

            assert assigned_shadow is not None
            assert assigned_shadow.name == "Test Persona"
            assert activity.status == ActivityStatus.ASSIGNED
            assert activity.assigned_shadow_id == assigned_shadow.id
