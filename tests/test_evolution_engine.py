import json
import pytest
from unittest.mock import patch, MagicMock

from systemu.core.models import Evolution, EvolutionType, EvolutionStatus, Shadow, ShadowStatus, Activity
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions", "elder"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "elder" / "memory_buffer.jsonl").write_text("", encoding="utf-8")
    return Vault(str(tmp_path))

@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.vault_dir = str(tmp_path)
    return cfg

def test_run_evolution_check_creates_proposals(tmp_vault, mock_config):
    # Add a dummy shadow to ensure vault is not empty
    shadow = Shadow(id="shadow_1", name="Test Shadow", description="Test", system_prompt="Test", status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)

    mock_llm_response = {
        "analysis_summary": "Test summary",
        "evolutions": [
            {
                "type": "upgrade",
                "entity_type": "shadow",
                "target_ids": ["shadow_1"],
                "description": "Upgrade shadow",
                "rationale": "Needs better logic"
            }
        ]
    }

    with patch("systemu.pipelines.evolution_engine.llm_call_json", return_value=mock_llm_response):
        with patch("systemu.pipelines.evolution_engine.notify_user", return_value="Approve"):
            from systemu.pipelines.evolution_engine import run_evolution_check
            evolutions = run_evolution_check(mock_config, tmp_vault)

            assert len(evolutions) == 1
            assert evolutions[0].evolution_type == EvolutionType.UPGRADE
            assert "shadow_1" in evolutions[0].target_entity_ids
            assert evolutions[0].status == EvolutionStatus.APPROVED # Approved by notify_user mock

def test_apply_shadow_upgrade(tmp_vault, mock_config):
    shadow = Shadow(id="shadow_1", name="Test Shadow", description="Test", system_prompt="Old prompt", status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)

    evo = Evolution(
        id=generate_id("evolution"),
        evolution_type=EvolutionType.UPGRADE,
        target_entity_type="shadow",
        target_entity_ids=["shadow_1"],
        description="Add calculation capability",
        rationale="Requested by user",
        status=EvolutionStatus.APPROVED
    )
    tmp_vault.save_evolution(evo)

    mock_llm_response = {
        "updated_system_prompt": "Old prompt. Also can calculate."
    }

    with patch("systemu.pipelines.evolution_engine._apply_shadow_upgrade") as mock_apply:
        pass
    # Wait, the local import is inside _apply_shadow_upgrade. So I should patch the original.
    with patch("systemu.core.llm_router.llm_call_json", return_value=mock_llm_response):
        from systemu.pipelines.evolution_engine import apply_evolution
        success = apply_evolution(evo.id, mock_config, tmp_vault)

        assert success is True
        updated_shadow = tmp_vault.get_shadow("shadow_1")
        assert updated_shadow.system_prompt == mock_llm_response["updated_system_prompt"]
        
        updated_evo = tmp_vault.get_evolution(evo.id)
        assert updated_evo.status == EvolutionStatus.APPLIED

def test_reflect_on_wild_card_proposes_skills_and_memory(tmp_vault, mock_config):
    activity = Activity(id="act_1", name="Test Act", scroll_id="scroll_1")
    shadow = Shadow(id="wc_1", name="Wild Card", description="Test", system_prompt="Test", status=ShadowStatus.AWAKENED)
    exec_result = {"execution_id": "exec_1", "status": "success"}

    mock_llm_response = {
        "proposed_skills": [
            {"name": "new_skill", "description": "New skill", "rationale": "Useful"}
        ],
        "memory_observations": [
            {"category": "Pattern", "observation": "Did a thing", "confidence": 0.9}
        ]
    }

    with patch("systemu.pipelines.evolution_engine.llm_call_json", return_value=mock_llm_response):
        from systemu.pipelines.evolution_engine import reflect_on_wild_card
        reflect_on_wild_card(shadow, activity, exec_result, tmp_vault, mock_config)

        evos = vault_evos = [tmp_vault.get_evolution(e["id"]) for e in tmp_vault.load_index("evolutions")]
        assert len(evos) == 1
        assert evos[0].evolution_type == EvolutionType.DISCOVER
        assert evos[0].target_entity_type == "skill"
        assert "new_skill" in evos[0].description

        # Check memory buffer
        from pathlib import Path
        mem_file = Path(tmp_vault.root) / "elder" / "memory_buffer.jsonl"
        assert mem_file.exists()
        lines = mem_file.read_text().strip().split("\n")
        assert len(lines) == 1
        mem_entry = json.loads(lines[0])
        assert mem_entry["observation"] == "Did a thing"
        assert mem_entry["exec_id"] == "exec_1"
