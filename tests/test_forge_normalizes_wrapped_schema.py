import json
import pytest
from unittest.mock import patch, MagicMock

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault


@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications", "executions"]:
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
def proposed_tool():
    return Tool(
        id=generate_id("tool"),
        name="seed_tool",
        description="A seed tool",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.PROPOSED,
    )


WRAPPED = {
    "type": "object",
    "properties": {
        "source_path": {"type": "string"},
        "password": {"type": "string"},
        "output_path": {"type": "string"},
    },
    "required": ["source_path", "password"],
}


def test_forge_tool_from_spec_normalizes_wrapped_schema(tmp_vault, mock_config, proposed_tool):
    tmp_vault.save_tool(proposed_tool)

    edited_spec = {
        "name": "encrypt_docx",
        "description": "Encrypt a docx",
        "parameters_schema": WRAPPED,
    }
    mock_llm_response = {"implementation": "def run(**params):\n    return {'success': True}\n"}

    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=mock_llm_response):
        from systemu.pipelines.tool_forge import forge_tool_from_spec
        tool = forge_tool_from_spec(proposed_tool.id, json.dumps(edited_spec), mock_config, tmp_vault)

    assert tool is not None
    # The stored schema must be the UNWRAPPED real param names, not the wrapper keys.
    assert set(tool.parameters_schema.keys()) == {"source_path", "password", "output_path"}
    assert "properties" not in tool.parameters_schema
    assert "type" not in tool.parameters_schema
    assert "required" not in tool.parameters_schema

    # The index header must surface the real names too.
    reloaded = tmp_vault.get_tool(tool.id)
    from systemu.vault.vault import _tool_header
    assert _tool_header(reloaded)["parameter_names"] == ["source_path", "password", "output_path"]
