import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from systemu.core.models import Activity, Tool, ToolStatus, ToolType, Scroll
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault

# --- Fixtures ---

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
    cfg.tier2_model = "test-model"
    return cfg

@pytest.fixture
def sample_proposed_tool():
    return Tool(
        id=generate_id("tool"),
        name="test_tool",
        description="A test tool",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.PROPOSED
    )

@pytest.fixture
def sample_scroll():
    return Scroll(
        id=generate_id("scroll"),
        name="test_scroll",
        source_session_id="test",
        raw_instructions_path="",
        narrative_md="test context"
    )

# --- Tests ---

def test_forge_proposed_tools_success(tmp_vault, mock_config, sample_proposed_tool, sample_scroll):
    tmp_vault.save_tool(sample_proposed_tool)
    tmp_vault.save_scroll(sample_scroll)
    
    activity = Activity(
        id="act_1", name="Test Act", scroll_id=sample_scroll.id,
        required_tool_ids=[sample_proposed_tool.id]
    )
    
    mock_llm_response = {
        "implementation": "def run():\n    return {'success': True}"
    }
    
    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=mock_llm_response):
        from systemu.pipelines.tool_forge import forge_proposed_tools
        forged = forge_proposed_tools(activity, mock_config, tmp_vault)
        
        assert len(forged) == 1
        tool = forged[0]
        assert tool.status == ToolStatus.FORGED
        assert tool.enabled is False
        
        impl_path = Path(mock_config.vault_dir) / "tools" / "implementations" / "test_tool.py"
        assert impl_path.exists()
        assert "def run():" in impl_path.read_text()

def test_preview_tool_code(mock_config, sample_proposed_tool, sample_scroll):
    mock_llm_response = {
        "implementation": "def run():\n    pass"
    }
    
    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=mock_llm_response):
        from systemu.pipelines.tool_forge import preview_tool_code
        code = preview_tool_code(sample_proposed_tool, sample_scroll, mock_config)
        assert code == "def run():\n    pass"

def test_save_approved_code(tmp_vault, mock_config, sample_proposed_tool):
    from systemu.pipelines.tool_forge import save_approved_code
    
    code = "def run():\n    return 'approved'"
    tool = save_approved_code(sample_proposed_tool, code, mock_config, tmp_vault)
    
    assert tool.status == ToolStatus.FORGED
    assert tool.enabled is False
    
    impl_path = Path(mock_config.vault_dir) / "tools" / "implementations" / "test_tool.py"
    assert impl_path.exists()
    assert code in impl_path.read_text()

def test_forge_tool_from_spec(tmp_vault, mock_config, sample_proposed_tool):
    tmp_vault.save_tool(sample_proposed_tool)
    
    edited_spec = {
        "name": "edited_tool",
        "description": "Edited description",
        "parameters_schema": {"new_param": {"type": "string"}}
    }
    
    mock_llm_response = {
        "implementation": "def run(new_param):\n    return {}"
    }
    
    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=mock_llm_response):
        from systemu.pipelines.tool_forge import forge_tool_from_spec
        tool = forge_tool_from_spec(sample_proposed_tool.id, json.dumps(edited_spec), mock_config, tmp_vault)
        
        assert tool is not None
        assert tool.name == "edited_tool"
        assert tool.description == "Edited description"
        assert "new_param" in tool.parameters_schema
        assert tool.status == ToolStatus.FORGED
        
        impl_path = Path(mock_config.vault_dir) / "tools" / "implementations" / "edited_tool.py"
        assert impl_path.exists()

def test_forge_fails_gracefully_on_empty_code(tmp_vault, mock_config, sample_proposed_tool, sample_scroll):
    # LLM returns empty implementation
    mock_llm_response = {"implementation": ""}
    
    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=mock_llm_response):
        from systemu.pipelines.tool_forge import _generate_and_save_code
        tool = _generate_and_save_code(sample_proposed_tool, sample_scroll, mock_config, tmp_vault)
        assert tool is None
