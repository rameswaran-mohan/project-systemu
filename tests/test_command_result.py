import json
from systemu.interface.command.result import CommandResult, CommandStatus


def test_ok_result_exit_code_zero():
    r = CommandResult(status=CommandStatus.OK, summary="Tool enabled", data={"tool_id": "tool_a"})
    assert r.exit_code == 0
    assert r.data["tool_id"] == "tool_a"


def test_error_result_exit_code_one():
    r = CommandResult(status=CommandStatus.ERROR, summary="not found")
    assert r.exit_code == 1


def test_queued_result_maps_to_exit_75():
    r = CommandResult(status=CommandStatus.QUEUED, summary="queued for review",
                      data={"decision_id": "dec_abc"})
    assert r.exit_code == 75


def test_to_json_is_machine_readable():
    r = CommandResult(status=CommandStatus.OK, summary="done", data={"n": 3})
    parsed = json.loads(r.to_json())
    assert parsed["status"] == "ok"
    assert parsed["exit_code"] == 0
    assert parsed["data"]["n"] == 3


def test_to_rich_returns_renderable_with_summary():
    r = CommandResult(status=CommandStatus.OK, summary="Tool enabled")
    rendered = r.to_rich()
    from rich.console import Console
    con = Console(record=True, width=80)
    con.print(rendered)
    assert "Tool enabled" in con.export_text()


def test_stream_ref_defaults_empty_and_round_trips():
    r = CommandResult(status=CommandStatus.OK, summary="x", stream_ref="job_1234")
    assert r.stream_ref == "job_1234"
    assert json.loads(r.to_json())["stream_ref"] == "job_1234"
