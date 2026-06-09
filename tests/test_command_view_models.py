from systemu.interface.command.view_models import ToolViewModel


def _sample_header():
    return {"id": "tool_a", "name": "fetch_json", "tool_type": "http",
            "status": "deployed", "description": "Fetch JSON from a URL",
            "enabled": True, "dry_run_status": "passed"}


def test_from_header_builds_view_model():
    vm = ToolViewModel.from_header(_sample_header())
    assert vm.id == "tool_a"
    assert vm.name == "fetch_json"
    assert vm.enabled is True


def test_to_row_matches_rich_table_columns():
    vm = ToolViewModel.from_header(_sample_header())
    row = vm.to_row()
    assert row == ["tool_a", "fetch_json", "http", "deployed", "Fetch JSON from a URL"]


def test_to_card_exposes_same_fields_as_row():
    vm = ToolViewModel.from_header(_sample_header())
    card = vm.to_card()
    assert card["id"] == "tool_a"
    assert card["status"] == "deployed"
    assert card["enabled"] is True
    assert card["name"] == "fetch_json" and card["type"] == "http"


def test_missing_description_renders_dash():
    h = _sample_header(); h["description"] = ""
    vm = ToolViewModel.from_header(h)
    assert vm.to_row()[4] == "—"
