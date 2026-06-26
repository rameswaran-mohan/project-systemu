from systemu.core.utils import generate_id
from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.vault.vault import _tool_header


def _tool(schema):
    return Tool(id=generate_id("tool"), name="encrypt_docx", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, parameters_schema=schema,
                status=ToolStatus.PROPOSED)


def test_tool_header_unwraps_wrapped_schema():
    h = _tool_header(_tool({
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "password": {"type": "string"},
            "output_path": {"type": "string"},
        },
        "required": ["source_path"],
    }))
    assert h["parameter_names"] == ["source_path", "password", "output_path"]
    assert "type" not in h["parameter_names"]
    assert "properties" not in h["parameter_names"]
    assert "required" not in h["parameter_names"]


def test_tool_header_flat_schema_still_works():
    h = _tool_header(_tool({"url": {"type": "string"}, "count": {"type": "integer"}}))
    assert h["parameter_names"] == ["url", "count"]
